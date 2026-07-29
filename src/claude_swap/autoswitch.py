"""Auto-switch engine: poll usage, switch accounts before they hit rate limits.

``AutoSwitchEngine`` is UI-agnostic — no printing, no argparse, no TUI
imports. It composes a :class:`ClaudeAccountSwitcher`, evaluates a threshold
policy each :meth:`~AutoSwitchEngine.tick`, and reports everything through
typed events handed to an ``on_event`` callback; the CLI renders them as
human lines or JSONL, and any future frontend (TUI dashboard, menubar) can
consume the same stream.

Policy in one paragraph: when the active account's *binding window* (the
higher of its 5h/7d utilization) crosses ``settings.threshold``, switch to
the candidate with the most headroom — proactively, so the old account is
still valid while a running Claude Code picks the new one up (this is what
makes the macOS ~30s Keychain cache latency harmless). Candidates must sit
``hysteresis_pct`` below the threshold so two accounts hovering at the line
never ping-pong, and a ``cooldown_seconds`` floor bounds the switch rate
(bypassed only when the active account is hard at its limit). Before
activation the target's token is *freshened* (refreshed if it expires within
10 minutes — twice Claude Code's refresh buffer, so a running Claude Code's
under-lock re-read sees a fresh token and aborts its own refresh); a target
whose refresh token is dead gets quarantined instead of activated. When the
active account's own usage becomes unreadable for ``unhealthy_ticks``
consecutive ticks, the engine fails over to any healthy candidate.

Cooldown and quarantine persist in ``<backup_root>/autoswitch_state.json``
(so cron-driven ``cswap auto --once`` ticks behave across processes), mutated
read-modify-write under a dedicated file lock.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import math
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

from claude_swap import oauth, poll_policy
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import SCHEMA_VERSION, USAGE_TOKEN_EXPIRED
from claude_swap.locking import FileLock
from claude_swap.poll_policy import (
    ESCALATION_MARGIN_PCT,
    RESET_SLACK_S,
    binding_pct,
)
from claude_swap.paced_selector import (
    material_usage_changed,
    rank_paced_slots,
    verified_eligible,
)
from claude_swap.settings import (
    DEFAULT_SLOT_POLICY,
    AutoSwitchSettings,
    SharedProfileSettings,
    SlotPolicy,
    atomic_write_json,
    load_shared_profile_settings,
    load_slot_policies,
    parse_model_names,
)
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import due_candidate, plan_oversleeps_interval

STATE_FILENAME = "autoswitch_state.json"
STATE_SCHEMA_VERSION = 1

_logger = logging.getLogger("claude-swap")

# Freshen targets whose access token expires within this window: twice Claude
# Code's own 5-minute refresh buffer, so its post-lock "abort refresh if not
# expired" re-read holds with margin after our swap.
FRESHEN_BUFFER_MS = 10 * 60 * 1000

# Sleep caps around a known quota reset (RESET_SLACK_S lives in poll_policy
# with the rest of the cadence numbers). Recheck at the exhausted-account poll
# cadence: providers can grant quota before the previously reported reset, and
# a long engine sleep must not suppress the fetch that discovers it.
MAX_SLEEP_S = poll_policy.EXHAUSTED_INTERVAL_S
NO_RESET_FALLBACK_S = 300.0

# Idle-hold cap (elapsed, not ticks — the hold itself slows the cadence to
# NO_RESET_FALLBACK_S): an owned-and-expired token normally means Claude Code
# is idle and will self-heal on next use, but a *dead* refresh token with an
# active user would look identical forever, so after this long the engine
# falls back to normal unhealthy counting.
IDLE_HOLD_MAX_S = 30 * 60.0

# Wedge-breaker: at or above this binding-window utilization the active
# account is effectively unusable, so force an at-limit switch (which skips
# the healthy-landing gate and takes any live account) even when the normal
# per-window thresholds or the consume-first landing filter would find no
# qualifying target. Without it an unattended `cswap auto` session could sit
# pinned to a maxed account until its next reset. 99 (not 100) leaves a tick
# of margin before a hard block.
ESCAPE_UTILIZATION_PCT = 99.0

# Adaptive scheduling: the baseline request volume is O(1) per tick — the
# active account plus ONE due candidate (stalest data first) — instead of
# every account in parallel, and the per-account cadence itself (movement,
# threshold distance, urgent mode, 429 recovery) lives in poll_policy, is
# persisted in the usage store by whichever collector fetched, and is shared
# by every surface. The engine escalates to a full candidate refresh only
# when a switch could actually be near: active utilization within
# ESCALATION_MARGIN_PCT of the threshold, or active usage unknown (failover
# needs fresh candidate data). The consume-first trigger can fire outside
# that escalation band; there it decides provisionally on the stored
# snapshot and escalates at commit time, when a switch would actually fire
# (the two-phase commit in _tick_inner).


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def pct_label(value: float) -> str:
    """A percentage for display, as configured: 85.555555 stays itself
    (never a rounded "85.5556") and 99.9 never becomes a lying "100" the
    way ``.0f`` renders it. Ten significant digits still absorb IEEE float
    noise (~15th digit) in computed utilizations (100.0 - headroom).
    Displayed comparisons must format BOTH sides with this helper — mixing
    formatters can render an impossible "85.5556% < 85.555555%"."""
    return f"{value:.10g}"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoSwitchEvent:
    """Base event. ``to_json()`` payloads are additive: consumers must ignore
    unknown ``event`` kinds and unknown fields."""

    kind: ClassVar[str] = "event"
    ts: str = field(default_factory=_now_iso, kw_only=True)

    def _fields(self) -> dict:
        return {}

    def to_json(self) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "event": self.kind,
            "ts": self.ts,
            **self._fields(),
        }

    def human(self) -> str:  # pragma: no cover - overridden
        return self.kind


@dataclass(frozen=True)
class PollEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "poll"
    active: dict | None  # account_ref shape, or None
    headroom: dict[str, float | None]  # account number → headroom pct (None=unknown)
    threshold: float
    # account number → last fetch-error cause ("http-429", "timeout", ...) for
    # accounts whose usage is unknown this tick. Additive field.
    fetch_errors: dict[str, str] = field(default_factory=dict)
    # account number → ordered window label → utilization pct ("5h", "7d",
    # then scoped model display names). Additive field: the binding pct alone
    # (e.g. "89%") hides which window binds — #115 was reported off that
    # ambiguity.
    windows: dict[str, dict[str, float]] = field(default_factory=dict)

    def _fields(self) -> dict:
        fields = {
            "active": self.active,
            "headroomPct": self.headroom,
            "threshold": self.threshold,
        }
        if self.fetch_errors:
            fields["fetchErrors"] = self.fetch_errors
        if self.windows:
            fields["windowsPct"] = self.windows
        return fields

    def _describe(self, num: str) -> str:
        wins = self.windows.get(num)
        if wins:
            return " · ".join(f"{name} {pct:.0f}%" for name, pct in wins.items())
        h = self.headroom.get(num)
        if h is not None:
            return f"{100 - h:.0f}%"
        err = self.fetch_errors.get(num)
        return f"? ({err})" if err else "?"

    def human(self) -> str:
        if self.active is None:
            return "poll: no active account"
        num = self.active.get("number")
        h = self.headroom.get(str(num))
        if h is not None:
            used = f"{100 - h:.0f}% used"
        else:
            err = self.fetch_errors.get(str(num))
            used = f"usage unknown ({err})" if err else "usage unknown"
        others = ", ".join(
            f"#{n}: {self._describe(n)}"
            for n in self.headroom
            if n != str(num)
        )
        tail = f" | others: {others}" if others else ""
        return (
            f"Account-{num} ({self.active.get('email')}): {used} "
            f"(switch at {pct_label(self.threshold)}%){tail}"
        )


@dataclass(frozen=True)
class SwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "switch"
    trigger: str  # "proactive" | "at-limit" | "failover" | "consume-first"
    from_ref: dict | None
    to_ref: dict | None
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    def _fields(self) -> dict:
        return {
            "trigger": self.trigger,
            "from": self.from_ref,
            "to": self.to_ref,
            "warnings": self.warnings,
            "dryRun": self.dry_run,
        }

    def human(self) -> str:
        src = (
            f"Account-{self.from_ref.get('number')}" if self.from_ref else "(none)"
        )
        dst = (
            f"Account-{self.to_ref.get('number')} ({self.to_ref.get('email')})"
            if self.to_ref
            else "?"
        )
        prefix = "[dry-run] would switch" if self.dry_run else "Switched"
        return f"{prefix} {src} -> {dst} ({self.trigger})"


@dataclass(frozen=True)
class NoSwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "no-switch"
    reason: str
    detail: str = ""

    def _fields(self) -> dict:
        return {"reason": self.reason, "detail": self.detail}

    def human(self) -> str:
        return f"no switch: {self.reason}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class QuarantineEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "account-quarantined"
    number: str
    email: str
    reason: str

    def _fields(self) -> dict:
        return {"number": self.number, "email": self.email, "reason": self.reason}

    def human(self) -> str:
        return (
            f"Account-{self.number} ({self.email}) quarantined: {self.reason}. "
            f"Log in with it and run 'cswap --add-account --slot {self.number}' "
            "to recover."
        )


@dataclass(frozen=True)
class UnquarantineEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "account-unquarantined"
    number: str
    email: str
    reason: str = "credentials-replaced"

    def _fields(self) -> dict:
        return {"number": self.number, "email": self.email, "reason": self.reason}

    def human(self) -> str:
        return f"Account-{self.number} ({self.email}) back in rotation ({self.reason})"


@dataclass(frozen=True)
class AllExhaustedEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "all-exhausted"
    earliest_reset_at: str | None

    def _fields(self) -> dict:
        return {"earliestResetAt": self.earliest_reset_at}

    def human(self) -> str:
        if self.earliest_reset_at:
            return f"all accounts exhausted; earliest reset {self.earliest_reset_at}"
        return "all accounts exhausted; no reset time known"


@dataclass(frozen=True)
class SleepEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "sleep"
    seconds: float
    until: str

    def _fields(self) -> dict:
        return {"seconds": round(self.seconds, 1), "until": self.until}

    def human(self) -> str:
        return f"sleeping {self.seconds / 60:.0f}m (until {self.until})"


@dataclass(frozen=True)
class ErrorEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "error"
    message: str
    transient: bool = True

    def _fields(self) -> dict:
        return {"message": self.message, "transient": self.transient}

    def human(self) -> str:
        return f"error: {self.message}" + (" (will retry)" if self.transient else "")


@dataclass(frozen=True)
class ConfigWarningEvent(AutoSwitchEvent):
    """A configuration value is syntactically fine but provably inert (e.g.
    an ``autoswitch.model`` name no account reports). Not an error: the
    engine keeps running on the axes that do exist."""

    kind: ClassVar[str] = "config-warning"
    message: str

    def _fields(self) -> dict:
        return {"message": self.message}

    def human(self) -> str:
        return f"warning: {self.message}"


@dataclass(frozen=True)
class SharedProfileSafetyEvent(AutoSwitchEvent):
    """Additive proof that a rollout or recovery gate held actuation closed."""

    kind: ClassVar[str] = "shared-profile-safety"
    reason: str
    rollout_stage: str
    worker_admission_ready: bool
    controller_phase: str | None = None
    manual_reconciliation_required: bool = True

    def _fields(self) -> dict:
        return {
            "reason": self.reason,
            "rolloutStage": self.rollout_stage,
            "workerAdmissionReady": self.worker_admission_ready,
            "controllerPhase": self.controller_phase,
            "manualReconciliationRequired": self.manual_reconciliation_required,
        }

    def human(self) -> str:
        return (
            "shared-profile actuation held: "
            f"{self.reason} (stage {self.rollout_stage})"
        )


@dataclass(frozen=True)
class SharedProfileActivationEvent(AutoSwitchEvent):
    """Locked ceiling proof paired with one committed shared-profile switch."""

    kind: ClassVar[str] = "shared-profile-activation-verified"
    trigger: str
    selection_epoch: str
    policy_revision: str
    controller_state: str
    active_slot: str
    target_slot: str
    target_identity: dict
    prelock_observation_revision: str
    locked_observation_revision: str
    prelock_fetched_at: float
    locked_fetched_at: float
    five_hour_pct: float
    weekly_pct: float
    five_hour_ceiling_pct: float
    weekly_ceiling_pct: float

    def _fields(self) -> dict:
        return {
            "trigger": self.trigger,
            "selectionEpoch": self.selection_epoch,
            "controllerRevision": self.selection_epoch,
            "controllerState": self.controller_state,
            "policyRevision": self.policy_revision,
            "activeSlot": self.active_slot,
            "targetSlot": self.target_slot,
            "targetIdentity": self.target_identity,
            "prelockObservationRevision": self.prelock_observation_revision,
            "lockedObservationRevision": self.locked_observation_revision,
            "prelockFetchedAt": self.prelock_fetched_at,
            "lockedFetchedAt": self.locked_fetched_at,
            "fiveHourPct": self.five_hour_pct,
            "weeklyPct": self.weekly_pct,
            "fiveHourCeilingPct": self.five_hour_ceiling_pct,
            "weeklyCeilingPct": self.weekly_ceiling_pct,
            "strictlyEligible": (
                self.five_hour_pct < self.five_hour_ceiling_pct
                and self.weekly_pct < self.weekly_ceiling_pct
            ),
        }

    def human(self) -> str:
        return (
            f"verified rotating seat {self.target_slot} for {self.trigger} "
            f"(epoch {self.selection_epoch[:12]})"
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TickOutcome(enum.Enum):
    """Outcome of one evaluation tick; values double as --once exit codes."""

    SWITCHED = 0
    ERROR = 1
    NO_ACTION = 2
    BLOCKED = 3  # wanted to switch but no viable target / all exhausted


@dataclass(frozen=True)
class CappedRecovery:
    """Complete current-epoch cap proof and its optional safe wake."""

    all_capped: bool
    wake_at: float | None = None


# Quarantine state persisted fingerprints from a local refresh-token-only
# helper; oauth.credential_fingerprint is identical for refresh-token creds.
# Setup-token quarantines stored None where the shared helper now yields a
# full-content hash — those release once on first recheck and re-quarantine on
# the next dead freshen (one harmless extra cycle, migration only).
_refresh_fingerprint = oauth.credential_fingerprint


def _window_pcts(
    usage: dict | None, models: tuple[str, ...] = ()
) -> dict[str, float]:
    """Ordered window label → pct: "5h", "7d", then configured scoped names.

    Deliberately restricted to the windows the *decision* reads (same
    ``models`` filter): showing an unconfigured scoped window at 100% next
    to a switch onto that account would look like a bug, when the engine
    correctly ignored it. Full per-model usage lives in ``cswap list``.
    """
    return {
        name: pct for name, pct, _ in oauth.relevant_windows(usage, models)
    }


# Reset math moved to poll_policy with the cadence numbers; aliased for the
# engine's sleep scheduling and the test suite.
_limiting_reset_ts = poll_policy.limiting_reset_ts
_earliest_future_reset_ts = poll_policy.earliest_future_reset_ts
_parse_reset_ts = poll_policy.parse_reset_ts


def _below_threshold_detail(
    active_pcts: dict[str, float],
    utilization: float,
    settings: "AutoSwitchSettings",
) -> str:
    """Human detail for a below-threshold hold.

    Reports the window nearest its own effective threshold (the next one that
    would trigger a switch) as ``pct% < threshold%``. With no per-window
    overrides every window shares ``threshold`` and the nearest is simply the
    binding (highest-utilization) window, reproducing the plain detail. Falls
    back to the binding utilization when no window pct is available.
    """
    if active_pcts:
        label = max(
            active_pcts,
            key=lambda lbl: active_pcts[lbl] - _window_threshold(lbl, settings),
        )
        return (
            f"{pct_label(active_pcts[label])}% < "
            f"{pct_label(_window_threshold(label, settings))}%"
        )
    return f"{pct_label(utilization)}% < {pct_label(settings.threshold)}%"


def _window_threshold(label: str, settings: "AutoSwitchSettings") -> float:
    """Effective trigger threshold for one usage window.

    5h/7d read their per-window overrides (falling back to the shared
    threshold); every other window (per-model scoped limits) uses the shared
    threshold, which has no per-window override in this feature.
    """
    if label == "5h":
        return settings.eff_5h()
    if label == "7d":
        return settings.eff_7d()
    return settings.threshold


def _seven_day_reset_ts(usage: dict | str | None, now: float) -> float | None:
    """Epoch of an account's 7-day (weekly) window reset, or None if unknown
    or already past.

    The consume-first strategy ranks by this — the weekly window is the
    perishable quota (the 5-hour one recycles too fast to be worth planning
    around). A stale snapshot can carry a ``resets_at`` that has since
    elapsed; treated as a real instant it would sort the *just-rolled-over*
    account (the least perishable quota of all) as "soonest", so past ==
    unknown. Plain ``ts <= now``: RESET_SLACK_S is poll-scheduling lag
    tolerance, not ranking input — padding here would turn a genuinely
    imminent reset into a false reset-unknown hold.
    """
    if isinstance(usage, dict):
        window = usage.get("seven_day")
        if isinstance(window, dict):
            ts = _parse_reset_ts(window.get("resets_at"))
            if ts is not None and ts > now:
                return ts
    return None


def _ref(number: str, email: str) -> dict:
    return {"number": int(number), "email": email}


def _headroom_by_account(
    usage: dict[str, dict | str | None], models: tuple[str, ...]
) -> dict[str, float | None]:
    """Per-account headroom derived from decision values."""
    return {
        num: oauth.account_headroom(
            value if isinstance(value, dict) else None, models
        )
        for num, value in usage.items()
    }


class AutoSwitchEngine:
    """Threshold-policy auto-switcher over a :class:`ClaudeAccountSwitcher`.

    ``on_event`` receives every :class:`AutoSwitchEvent`; exceptions it raises
    are not caught (a broken frontend should fail loudly in tests). ``clock``
    is wall time (persisted cooldown timestamps must survive processes).
    """

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        settings: AutoSwitchSettings,
        on_event: Callable[[AutoSwitchEvent], None],
        *,
        dry_run: bool = False,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        worker_admission_ready: Callable[[], bool] | None = None,
        manual_reconcile: bool = False,
    ):
        self.switcher = switcher
        self.settings = settings
        # Model(s) whose per-model weekly limit also binds the switch decision
        # (empty = account-wide 5h/7d only). ``settings.model`` is a comma-
        # separated list ("Fable", "Opus,Sonnet", "all"); parse once here and
        # pass everywhere usage windows are read — decisions, cadence, and
        # reset scheduling must all see the same axes.
        self._models = parse_model_names(settings.model)
        # Poll plans written by the collector must key on the same threshold/
        # models the engine decides with (CLI overrides included), not on
        # whatever the settings file happens to say.
        switcher.set_poll_policy_inputs(settings.threshold, self._models)
        self.on_event = on_event
        self.dry_run = dry_run
        self.state_path = state_path or (switcher.backup_dir / STATE_FILENAME)
        self.clock = clock
        # This deployment's shared queue runs continuously and self-wakes, so
        # there is no pause/ack admission protocol to wait for. An integration
        # may still inject an explicit readiness probe; its False/error result
        # retains the fail-closed hold.
        self._worker_admission_ready = worker_admission_ready or (lambda: True)
        self._manual_reconcile = manual_reconcile
        self._stop = threading.Event()
        # Cuts the current inter-tick sleep short (a session threshold change
        # from the TUI should show a fresh decision now, not next interval).
        self._wake = threading.Event()
        self._unhealthy_ticks = 0
        # Both set per tick: a known-reset sleep target, and whether a BLOCKED
        # outcome is static enough (truly exhausted / no candidates) to wait
        # longer than the normal interval.
        self._sleep_until_ts: float | None = None
        self._blocked_wait_long = False
        # Idle-hold: when the active token expired while Claude Code owns it
        # (and is therefore idle), crawl instead of counting unhealthy ticks.
        # ``_idle_hold_since`` survives across ticks (elapsed-time cap);
        # ``_idle_hold_slow`` is per-tick like ``_blocked_wait_long``.
        self._idle_hold_since: float | None = None
        self._idle_hold_slow = False
        # One-shot typo guard for ``autoswitch.model``: resolved (and possibly
        # warned) on the first tick where every relevant account has readable
        # usage — adaptive polling legitimately leaves gaps before that.
        self._model_check_done = not self._models

    # -- state file ---------------------------------------------------------

    def _state_lock(self) -> FileLock:
        return FileLock(self.state_path.parent / ".autoswitch_state.lock")

    def _read_state(self) -> dict:
        return self._read_state_checked()[0]

    def _read_state_checked(self) -> tuple[dict, bool]:
        """Read once, preserving whether an existing artifact was valid."""
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, True
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}, False
        return (raw, True) if isinstance(raw, dict) else ({}, False)

    def _hold_shared_profile(
        self,
        *,
        reason: str,
        shared: SharedProfileSettings,
        controller: object,
        worker_ready: bool,
        persist: bool,
        manual_reconciliation_required: bool = True,
    ) -> TickOutcome:
        phase = controller.get("phase") if isinstance(controller, dict) else None
        if persist and not self.dry_run:
            blocked = {
                **(controller if isinstance(controller, dict) else {}),
                "phase": "verification-blocked",
                "reason": reason,
                "rolloutStage": shared.rollout_stage,
                "manualReconciliationRequired": manual_reconciliation_required,
            }
            self._store_controller(blocked)
        self._emit(
            SharedProfileSafetyEvent(
                reason=reason,
                rollout_stage=shared.rollout_stage,
                worker_admission_ready=worker_ready,
                controller_phase=phase,
                manual_reconciliation_required=manual_reconciliation_required,
            )
        )
        self._emit(NoSwitchEvent(reason=reason))
        return TickOutcome.BLOCKED

    def _mutate_state(self, mutator: Callable[[dict], None]) -> dict:
        """Read-modify-write the state file under its lock; returns new state.

        The lock prevents two concurrent engines (loop + cron ``--once``) from
        overwriting each other's quarantine/cooldown updates. Never called
        while any other lock is held.
        """
        with self._state_lock():
            state = self._read_state()
            state["schemaVersion"] = STATE_SCHEMA_VERSION
            mutator(state)
            atomic_write_json(self.state_path, state)
            return state

    # -- quarantine -----------------------------------------------------------

    def _quarantine(self, number: str, email: str, reason: str) -> None:
        creds = self.switcher.read_account_credentials(number, email)
        fingerprint = _refresh_fingerprint(creds) if creds else None

        def add(state: dict) -> None:
            state.setdefault("quarantine", {})[number] = {
                "email": email,
                "reason": reason,
                "at": _now_iso(),
                "refreshTokenFingerprint": fingerprint,
            }

        self._mutate_state(add)
        self._emit(QuarantineEvent(number=number, email=email, reason=reason))

    def _release_recovered_quarantines(self, state: dict) -> dict:
        """Drop quarantine entries whose credential was replaced since.

        A changed refresh-token fingerprint (or a removed/re-added slot) means
        the user re-logged in and re-captured the account — the dead lineage
        is gone, so it re-enters rotation.
        """
        quarantine = state.get("quarantine")
        if not isinstance(quarantine, dict) or not quarantine:
            return state
        to_release: list[tuple[str, str, str]] = []
        for number, entry in quarantine.items():
            email_now = self.switcher.account_email(number)
            if not email_now or email_now != entry.get("email"):
                to_release.append(
                    (number, entry.get("email", ""), "account-replaced")
                )
                continue
            creds = self.switcher.read_account_credentials(number, email_now)
            fingerprint = _refresh_fingerprint(creds) if creds else None
            if fingerprint != entry.get("refreshTokenFingerprint"):
                to_release.append((number, email_now, "credentials-replaced"))
        if not to_release:
            return state

        def drop(s: dict) -> None:
            q = s.get("quarantine")
            if isinstance(q, dict):
                for number, _, _ in to_release:
                    q.pop(number, None)

        state = self._mutate_state(drop)
        for number, email, reason in to_release:
            self._emit(UnquarantineEvent(number=number, email=email, reason=reason))
        return state

    # -- freshening -----------------------------------------------------------

    def _freshen_target(self, number: str, email: str) -> str:
        """Ensure a candidate's stored token outlives Claude Code's 5-min
        refresh buffer before it gets activated.

        Returns ``"ok"``, ``"invalid_grant"`` (dead lineage — quarantine),
        ``"identity-conflict"`` (alive but authenticates as a different
        account — quarantine, do not activate), ``"transient"`` (network
        trouble — try again next tick) or ``"skip-live-session"``. Only ever
        touches the slot's *backup* store; the active credential belongs to
        Claude Code.
        """
        if self.switcher.account_kind_for(number) == "api_key":
            return "ok"  # API keys don't expire/refresh
        if self.switcher.live_session_pids_for(number, email):
            # A live `cswap run` session owns this account's token in its own
            # profile. Auto-activating it as the default login too would put
            # one rotating refresh token in two config dirs (the stale-copy
            # failure class) with nobody reading the warning — and its quota
            # is already being consumed by that session anyway. Manual
            # switch_to keeps its warn-and-proceed behavior; auto skips.
            return "skip-live-session"
        creds = self.switcher.read_account_credentials(number, email)
        if not creds:
            return "transient"
        data = oauth.extract_oauth_data(creds)
        if not data:
            return "invalid_grant"
        expires_at = data.get("expiresAt")
        now_ms = self.clock() * 1000
        near_expiry = (
            isinstance(expires_at, (int, float))
            and now_ms + FRESHEN_BUFFER_MS >= expires_at
        )
        if not near_expiry:
            return "ok"
        outcome = oauth.try_refresh_oauth_credentials(creds)
        if outcome.error is None and outcome.credentials:
            # Persist first, unconditionally: the grant consumed a generation,
            # and not writing the successor would kill the lineage regardless
            # of whose it turns out to be.
            self.switcher.persist_backup_credentials(
                number, email, outcome.credentials
            )
            if self._note_token_identity(number, outcome.token_account):
                # The slot's stored credential authenticates as a *different*
                # account — activating it would put the user on the wrong
                # account with every gauge reading normal. Not a viable
                # target; the caller quarantines it (released automatically
                # once the credential is replaced by a re-add).
                return "identity-conflict"
            return "ok"
        if outcome.error in ("invalid_grant", "no_refresh_token"):
            return "invalid_grant"
        return "transient"

    def _note_token_identity(
        self, number: str, token_account: dict | None
    ) -> bool:
        """Use the token endpoint's free identity to verify/backfill a slot.

        The refresh grant just ran against the slot's own stored credential,
        so ``token_account`` (when the server includes it) names who that
        credential really is. Returns True on a *conflict*: the credential
        authenticates under a different organization than the slot records
        (org compared first, whenever both sides record one), or as a
        different account uuid. An empty slot uuid (blank-uuid records from
        older versions, add-token placeholders) is backfilled — but only
        when no org conflict exists: a wrong-org credential is evidence the
        slot holds the wrong account, and backfilling *its* uuid would
        poison the slot's identity record (backfill never rewrites a
        non-empty uuid, so that corruption would be sticky).

        ``_parse_token_account`` already enforces a strict boundary, but this
        identity is opportunistic — re-check types here so malformed data can
        never break the freshen that carried it (the successor credential is
        already persisted by the time this runs).
        """
        if not isinstance(token_account, dict):
            return False
        ta_uuid = token_account.get("uuid")
        if not isinstance(ta_uuid, str) or not ta_uuid.strip():
            return False
        ta_uuid = ta_uuid.strip()
        slot_identity = self.switcher.account_identity(number)
        ta_org = token_account.get("organizationUuid")
        slot_org = slot_identity.get("organizationUuid") or ""
        if isinstance(ta_org, str) and ta_org and slot_org and ta_org != slot_org:
            return True
        if not slot_identity.get("uuid"):
            try:
                self.switcher.backfill_account_uuid(number, ta_uuid)
            except Exception as e:  # never let bookkeeping break a freshen
                _logger.debug("uuid backfill failed for account %s: %r", number, e)
            return False
        return slot_identity["uuid"] != ta_uuid

    # -- tick -----------------------------------------------------------------

    def tick(self) -> TickOutcome:
        """Evaluate once: poll usage, maybe switch. Never raises."""
        try:
            return self._tick_inner()
        except ClaudeSwitchError as e:
            self._emit(ErrorEvent(message=str(e), transient=True))
            return TickOutcome.ERROR
        except Exception as e:  # pragma: no cover - safety net
            self._emit(
                ErrorEvent(message=f"{type(e).__name__}: {e}", transient=True)
            )
            return TickOutcome.ERROR

    def _tick_inner(self) -> TickOutcome:
        self._sleep_until_ts = None
        self._blocked_wait_long = False
        self._idle_hold_slow = False
        settings = self.settings
        state, state_readable = self._read_state_checked()
        if not self.dry_run:
            # Dry-run must not write anything, so recovered quarantines are
            # only released (state mutation) on real ticks.
            state = self._release_recovered_quarantines(state)
        quarantined = set(
            state.get("quarantine", {})
            if isinstance(state.get("quarantine"), dict)
            else {}
        )

        current = self.switcher.current_account_number()
        if current is None:
            self._emit(
                PollEvent(active=None, headroom={}, threshold=settings.threshold)
            )
            if self.switcher.has_live_login():
                # Live login exists but cswap doesn't manage it: never act —
                # a switch would overwrite it without a backup.
                self._emit(
                    NoSwitchEvent(
                        reason="unmanaged-active-account",
                        detail="run 'cswap --add-account' to include it in rotation",
                    )
                )
            else:
                self._emit(
                    NoSwitchEvent(
                        reason="no-active-account",
                        detail="log in and run 'cswap --add-account' first",
                    )
                )
            return TickOutcome.NO_ACTION

        current_email = self.switcher.account_email(current)
        active_ref = _ref(current, current_email) if current_email else {
            "number": int(current),
            "email": "",
        }

        shared = load_shared_profile_settings(self.switcher.backup_dir)
        controller = state.get("sharedProfileController")
        if shared.manual_hold:
            return self._hold_shared_profile(
                reason="operator-manual-hold",
                shared=shared,
                controller=controller,
                worker_ready=False,
                persist=True,
            )
        if shared.enabled:
            if not state_readable:
                return self._hold_shared_profile(
                    reason="controller-state-unreadable",
                    shared=shared,
                    controller=None,
                    worker_ready=False,
                    persist=False,
                )
            phase = (
                controller.get("phase")
                if isinstance(controller, dict)
                else None
            )
            if phase == "verification-blocked" and not self._manual_reconcile:
                return self._hold_shared_profile(
                    reason="verification-blocked",
                    shared=shared,
                    controller=controller,
                    worker_ready=False,
                    persist=True,
                )
            if phase == "selecting" and not self._manual_reconcile:
                return self._hold_shared_profile(
                    reason="interrupted-selection-manual-reconciliation",
                    shared=shared,
                    controller=controller,
                    worker_ready=False,
                    persist=True,
                )
            if shared.rollout_stage == "contract":
                return self._hold_shared_profile(
                    reason="rollout-contract",
                    shared=shared,
                    controller=controller,
                    worker_ready=False,
                    persist=False,
                    manual_reconciliation_required=False,
                )
            if shared.rollout_stage == "shadow" and not self.dry_run:
                return self._hold_shared_profile(
                    reason="shadow-requires-dry-run",
                    shared=shared,
                    controller=controller,
                    worker_ready=False,
                    persist=False,
                    manual_reconciliation_required=False,
                )
            try:
                worker_ready = (
                    False
                    if shared.rollout_stage == "shadow"
                    else bool(self._worker_admission_ready())
                )
            except Exception:
                worker_ready = False
            if shared.rollout_stage != "shadow" and not worker_ready:
                return self._hold_shared_profile(
                    reason="worker-admission-hold-unavailable",
                    shared=shared,
                    controller=controller,
                    worker_ready=False,
                    persist=True,
                )
            return self._tick_shared_profile(
                current=current,
                current_email=current_email,
                state=state,
                shared=shared,
                quarantined=quarantined,
            )

        entries, usage, headroom = self._collect_scheduled_usage(
            current, quarantined, threshold=settings.min_effective_threshold()
        )
        self._emit(
            PollEvent(
                active=active_ref,
                headroom=headroom,
                threshold=settings.threshold,
                fetch_errors={
                    num: entry.last_error
                    for num, entry in entries.items()
                    if usage.get(num) is None and entry.last_error
                },
                windows={
                    num: pcts
                    for num, value in usage.items()
                    if (pcts := _window_pcts(
                        value if isinstance(value, dict) else None, self._models
                    ))
                },
            )
        )

        if not self._model_check_done:
            self._check_model_names(quarantined, usage)

        if (
            self.switcher.account_kind_for(current) == "api_key"
            and not settings.include_api_key_accounts
        ):
            self._emit(
                NoSwitchEvent(
                    reason="active-api-key",
                    detail="API-key accounts have no quota to watch",
                )
            )
            return TickOutcome.NO_ACTION

        active_headroom = headroom.get(current)
        if active_headroom is not None:
            self._unhealthy_ticks = 0
            self._idle_hold_since = None
            utilization = 100.0 - active_headroom
            # Per-window trigger: each window crosses its own effective
            # threshold (5h/7d overrides, or the shared threshold for scoped
            # windows). With no overrides every window keys off ``threshold``,
            # so ``over_threshold`` reduces to the old binding-window test
            # (max-fold utilization >= threshold).
            active_pcts = _window_pcts(
                usage.get(current) if isinstance(usage.get(current), dict) else None,
                self._models,
            )
            over_threshold = any(
                pct >= _window_threshold(label, settings)
                for label, pct in active_pcts.items()
            )
            if not over_threshold:
                if settings.strategy != "consume-first":
                    self._emit(
                        NoSwitchEvent(
                            reason="below-threshold",
                            # Both sides through pct_label: .0f utilization could
                            # display an impossible "100% < 99.9%".
                            detail=_below_threshold_detail(
                                active_pcts, utilization, settings
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # consume-first: below the threshold we still proactively move to
                # whichever account's weekly window resets soonest, to burn the
                # most-perishable quota first. Candidate selection decides whether
                # a sooner-resetting account with room actually exists.
                trigger = "consume-first"
            else:
                trigger = "at-limit" if active_headroom <= 0 else "proactive"
        else:
            if usage.get(current) == USAGE_TOKEN_EXPIRED:
                # Expired and the refresh could not complete this pass (lock
                # contention, unattributable lineage, failed persist, or the
                # row's failure backoff gating the fetch). The locked-refresh
                # path retries on later passes — no quota burn, nothing to
                # switch for yet; crawl slowly instead of burning failover
                # ticks (Finding 2 of the usage-lapse investigation).
                now = self.clock()
                if self._idle_hold_since is None:
                    self._idle_hold_since = now
                if now - self._idle_hold_since <= IDLE_HOLD_MAX_S:
                    self._unhealthy_ticks = 0
                    self._idle_hold_slow = True
                    self._emit(
                        NoSwitchEvent(
                            reason="active-idle",
                            detail=(
                                "token expired while Claude Code is idle; "
                                "resumes on next use"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # Held far longer than any idle nap should need — likely a
                # dead refresh token with an *active* user. Fall through to
                # normal unhealthy counting so failover can still happen.
                _logger.warning(
                    "Active token expired and owned for over %.0f minutes; "
                    "resuming unhealthy counting (dead refresh token?)",
                    IDLE_HOLD_MAX_S / 60,
                )
            else:
                self._idle_hold_since = None
            self._unhealthy_ticks += 1
            if self._unhealthy_ticks < settings.unhealthy_ticks:
                self._emit(
                    NoSwitchEvent(
                        reason="active-usage-unknown",
                        detail=(
                            f"{self._unhealthy_ticks}/{settings.unhealthy_ticks} "
                            "before failover"
                        ),
                    )
                )
                return TickOutcome.NO_ACTION
            trigger = "failover"

        if trigger in ("proactive", "consume-first") and self._in_cooldown(state):
            self._emit(NoSwitchEvent(reason="cooldown"))
            return TickOutcome.NO_ACTION

        # -- candidate selection ------------------------------------------
        candidates = [
            num
            for num in self.switcher.switchable_account_numbers()
            if num != current and num not in quarantined
        ]
        oauth_candidates = [
            n for n in candidates if self.switcher.account_kind_for(n) != "api_key"
        ]
        api_key_candidates = (
            [n for n in candidates if self.switcher.account_kind_for(n) == "api_key"]
            if settings.include_api_key_accounts
            else []
        )
        if (
            trigger == "consume-first"
            and not oauth_candidates
            and active_headroom is not None
        ):
            # Healthy below-threshold account with no OAuth peer to compare
            # against — the same state `best` reports as below-threshold
            # NO_ACTION before ever reaching candidate selection. API-key
            # candidates don't change the outcome: they have no weekly window
            # to consume, so a consume-first nudge never targets them. Keep
            # the exit-code contract identical across strategies: cron
            # wrappers keying on BLOCKED must not see false "blocked" from
            # the flag alone.
            self._emit(
                NoSwitchEvent(
                    reason="below-threshold",
                    detail=(
                        f"{pct_label(100.0 - active_headroom)}% < "
                        f"{pct_label(settings.threshold)}%"
                    ),
                )
            )
            return TickOutcome.NO_ACTION
        if not oauth_candidates and not api_key_candidates:
            # Won't change until the user adds/recovers an account — no point
            # re-polling at full cadence.
            self._blocked_wait_long = True
            self._emit(NoSwitchEvent(reason="no-candidates"))
            return TickOutcome.BLOCKED

        consume_first = settings.strategy == "consume-first"
        ordered, any_known, active_reset_ts = self._rank_candidates(
            trigger=trigger,
            consume_first=consume_first,
            oauth_candidates=oauth_candidates,
            usage=usage,
            headroom=headroom,
            current=current,
            active_headroom=active_headroom,
            settings=settings,
            now=self.clock(),
        )

        if trigger == "consume-first" and ordered:
            # Two-phase commit: the provisional pick may have ridden a
            # snapshot up to CANDIDATE_MAX_INTERVAL_S stale — consume-first
            # decides below the threshold, where the collector only escalates
            # inside the ESCALATION_MARGIN_PCT band (flat-traffic invariant).
            # A switch is imminent, so spend the fetches now and re-decide on
            # fresh data.
            # reserve() serves just-fetched accounts from the store, so this
            # is cheap in-tick and plan-bounded across ticks. The trigger is
            # deliberately NOT re-classified if the fresh active crossed the
            # threshold: a still-qualifying sooner target switches anyway,
            # and otherwise the next tick escalates normally and escapes.
            entries = self.switcher.usage_entries_by_account(
                fetch={current, *candidates}
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}
            headroom = _headroom_by_account(usage, self._models)
            active_headroom = headroom.get(current)
            ordered, any_known, active_reset_ts = self._rank_candidates(
                trigger=trigger,
                consume_first=consume_first,
                oauth_candidates=oauth_candidates,
                usage=usage,
                headroom=headroom,
                current=current,
                active_headroom=active_headroom,
                settings=settings,
                now=self.clock(),
            )

        if (
            not ordered
            and trigger == "proactive"
            and active_headroom is not None
            and (100.0 - active_headroom) >= ESCAPE_UTILIZATION_PCT
        ):
            # Wedge-breaker: a proactive tick where the active account is
            # critically high (>= the escape line, but short of the 100% that
            # would already classify at-limit) yet no candidate cleared the
            # normal landing gate. Holding to the next reset would strand an
            # unattended session, so fall back to an at-limit rank (landing
            # gate skipped) and take the best live account — least-bad beats
            # wedged. Deliberately not for the consume-first trigger: that path
            # never re-classifies mid-tick (a 100% active escapes on the next
            # tick's normal at-limit classification).
            escaped, any_known, active_reset_ts = self._rank_candidates(
                trigger="at-limit",
                consume_first=consume_first,
                oauth_candidates=oauth_candidates,
                usage=usage,
                headroom=headroom,
                current=current,
                active_headroom=active_headroom,
                settings=settings,
                now=self.clock(),
            )
            if escaped:
                ordered = escaped
                trigger = "at-limit"

        if not ordered and api_key_candidates and trigger != "consume-first":
            # Last resort when we must move: metered API-key accounts
            # (unmeasurable headroom). Never for a below-threshold consume-first
            # nudge — those API-key accounts have no weekly window to consume.
            ordered = api_key_candidates

        if not ordered:
            if not any_known:
                # No candidate readable this tick — true for every strategy,
                # and must not be dressed up as a consume-first hold.
                self._emit(
                    NoSwitchEvent(
                        reason="no-comparison",
                        detail="no candidate has readable usage",
                    )
                )
                return TickOutcome.BLOCKED
            if trigger == "consume-first":
                # Below the threshold and healthy: staying put is a correct
                # outcome, never a block. Distinguish *why* nothing qualified
                # so an opted-in user can see the strategy working (or inert).
                if active_reset_ts is None:
                    # The strictly-sooner filter skips every candidate when the
                    # active account's weekly reset is unknown — without this
                    # reason the strategy would look enabled while doing
                    # nothing, with no way to tell.
                    self._emit(
                        NoSwitchEvent(
                            reason="reset-unknown",
                            detail=(
                                "active account's weekly reset time is "
                                "unknown; consume-first is idle until it "
                                "is reported"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # Covers both "everyone resets later" and "sooner ones have no
                # room" — don't claim the active account resets first when the
                # real story may be exhausted candidates.
                self._emit(
                    NoSwitchEvent(
                        reason="already-consuming-soonest",
                        detail="no sooner-resetting account with room to spare",
                    )
                )
                return TickOutcome.NO_ACTION
            # "All exhausted" (and its bounded reset-aware sleep) only when it's
            # literally true: every candidate's usage is known and at its
            # limit. A candidate that merely failed the proactive hysteresis
            # gate, or one whose usage is unreadable this tick, can become
            # viable at any moment — and the active account can hit 100% and
            # need the at-limit escape — so those keep the normal cadence.
            candidate_headrooms = [headroom.get(n) for n in oauth_candidates]
            truly_exhausted = all(
                h is not None and h <= 0 for h in candidate_headrooms
            )
            if not truly_exhausted:
                self._emit(
                    NoSwitchEvent(
                        reason="no-qualifying-candidate",
                        detail=(
                            "no candidate is below the threshold and better "
                            "than the active account by the hysteresis "
                            "margin, or usage is unreadable this tick"
                        ),
                    )
                )
                return TickOutcome.BLOCKED
            self._blocked_wait_long = True
            earliest = self._earliest_recovery(usage)
            if earliest is not None:
                self._sleep_until_ts = earliest.timestamp() + RESET_SLACK_S
            self._emit(
                AllExhaustedEvent(
                    earliest_reset_at=(
                        earliest.isoformat().replace("+00:00", "Z")
                        if earliest
                        else None
                    )
                )
            )
            return TickOutcome.BLOCKED

        # -- freshen + switch ----------------------------------------------
        transient_failure = False
        for num in ordered:
            email = self.switcher.account_email(num)
            if trigger == "consume-first":
                # The phase-2 refetch is best-effort: the collector refuses
                # accounts in failure backoff or claimed by a concurrent
                # poller, which then serve their stored entries. Consume-first
                # is opportunistic, not an escape — never act on stale data
                # or slide to a worse-ranked target; hold and retry next tick.
                entry = entries.get(num)
                if entry is None or not entry.fresh(self.clock()):
                    self._emit(
                        NoSwitchEvent(
                            reason="stale-usage",
                            detail=(
                                f"account {num} usage could not be refreshed "
                                "this tick (backoff or a concurrent poller); "
                                "retrying"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
            if self.dry_run:
                # Dry-run stops at the decision: no token refresh, no
                # quarantine writes — freshening is a mutation.
                return self._perform(num, email, trigger)
            status = self._freshen_target(num, email)
            if status == "identity-conflict":
                # The slot's credential is alive but belongs to a different
                # account — switching onto it would silently run the wrong
                # account. Quarantine (auto-released once a re-add replaces
                # the credential).
                self._quarantine(num, email, "identity-conflict")
                continue
            if status == "invalid_grant":
                self._quarantine(num, email, "invalid_grant")
                continue
            if status == "transient":
                transient_failure = True
                continue
            if status == "skip-live-session":
                continue
            return self._perform(num, email, trigger)

        if transient_failure:
            self._emit(
                ErrorEvent(
                    message="could not freshen any candidate (network?)",
                    transient=True,
                )
            )
            return TickOutcome.ERROR
        self._emit(NoSwitchEvent(reason="no-viable-target"))
        return TickOutcome.BLOCKED

    # -- shared-profile rotation controller ---------------------------------

    @staticmethod
    def _policy_revision(
        policies: dict[str, SlotPolicy], shared: SharedProfileSettings
    ) -> str:
        payload = {
            "dwellSeconds": shared.dwell_seconds,
            "materialUsageDeltaPct": shared.material_usage_delta_pct,
            "slots": {
                slot: {
                    "fiveHourCeilingPct": policy.five_hour_ceiling_pct,
                    "weeklyCeilingPct": policy.weekly_ceiling_pct,
                    "priority": policy.priority,
                }
                for slot, policy in sorted(
                    policies.items(), key=lambda item: int(item[0])
                )
            },
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _emit_shared_activation_proof(
        self,
        *,
        trigger: str,
        selection_epoch: str,
        policy_revision: str,
        controller_state: str,
        target: str,
        prelock_usage: dict,
        locked_usage: dict,
        prelock_fetched_at: float,
        locked_fetched_at: float,
        policy: SlotPolicy,
    ) -> None:
        self._emit(
            SharedProfileActivationEvent(
                trigger=trigger,
                selection_epoch=selection_epoch,
                policy_revision=policy_revision,
                controller_state=controller_state,
                active_slot=target,
                target_slot=target,
                target_identity=self.switcher.account_identity(target),
                prelock_observation_revision=(
                    f"{selection_epoch}:candidate:{prelock_fetched_at:.9f}"
                ),
                locked_observation_revision=(
                    f"{selection_epoch}:locked-recheck:{locked_fetched_at:.9f}"
                ),
                prelock_fetched_at=prelock_fetched_at,
                locked_fetched_at=locked_fetched_at,
                five_hour_pct=float(locked_usage["five_hour"]["pct"]),
                weekly_pct=float(locked_usage["seven_day"]["pct"]),
                five_hour_ceiling_pct=policy.five_hour_ceiling_pct,
                weekly_ceiling_pct=policy.weekly_ceiling_pct,
            )
        )

    def _shared_policies(
        self, quarantined: set[str]
    ) -> dict[str, SlotPolicy]:
        explicit = load_slot_policies(self.switcher.backup_dir)
        return {
            slot: explicit.get(int(slot), DEFAULT_SLOT_POLICY)
            for slot in self.switcher.switchable_account_numbers()
            if slot not in quarantined
            and self.switcher.account_kind_for(slot) != "api_key"
        }

    def _steady_controller(
        self,
        slot: str,
        usage: dict,
        policies: dict[str, SlotPolicy],
        shared: SharedProfileSettings,
        *,
        primed_slots: list[str] | None = None,
        controller_revision: str | None = None,
    ) -> dict:
        now = self.clock()
        primed = sorted(
            policies if primed_slots is None else primed_slots,
            key=int,
        )
        return {
            "phase": "steady",
            "activeSlot": slot,
            "identity": self.switcher.account_identity(slot),
            "policyRevision": self._policy_revision(policies, shared),
            "controllerRevision": controller_revision,
            "activatedAt": now,
            "dwellUntil": now + shared.dwell_seconds,
            "activationUsage": usage,
            **self._priming_progress(primed),
        }

    def _store_controller(self, controller: dict) -> None:
        if self.dry_run:
            return

        def update(state: dict) -> None:
            state["sharedProfileController"] = controller

        self._mutate_state(update)

    def _primed_slots(
        self,
        controller: object,
        policies: dict[str, SlotPolicy],
    ) -> list[str]:
        if not isinstance(controller, dict):
            return []
        raw = controller.get("primedSlots")
        if not isinstance(raw, list):
            return []
        primed = [
            slot
            for slot in raw
            if isinstance(slot, str) and slot in policies
        ]
        recorded = controller.get("primedIdentities")
        # ``priming-complete`` is the one migration marker used by the
        # pre-priming controller tests. New operational state always binds a
        # primed slot to its identity so a move/swap cannot inherit proof.
        if not isinstance(recorded, dict):
            return (
                sorted(set(primed), key=int)
                if controller.get("phase") == "priming-complete"
                else []
            )
        return sorted(
            {
                slot
                for slot in primed
                if recorded.get(slot) == self.switcher.account_identity(slot)
            },
            key=int,
        )

    def _priming_progress(self, primed_slots: list[str]) -> dict:
        return {
            "primedSlots": primed_slots,
            "primedIdentities": {
                slot: self.switcher.account_identity(slot)
                for slot in primed_slots
            },
        }

    @staticmethod
    def _five_hour_reset(usage: object) -> str | None:
        if not isinstance(usage, dict):
            return None
        window = usage.get("five_hour")
        if not isinstance(window, dict):
            return None
        value = window.get("resets_at")
        return value if isinstance(value, str) else None

    @staticmethod
    def _five_hour_reset_is_valid_or_missing(usage: object) -> bool:
        if not isinstance(usage, dict):
            return False
        window = usage.get("five_hour")
        if not isinstance(window, dict):
            return False
        if "resets_at" not in window:
            return True
        return _parse_reset_ts(window.get("resets_at")) is not None

    @classmethod
    def _verified_priming_baseline(
        cls, usage: object, policy: SlotPolicy
    ) -> bool:
        return verified_eligible(
            usage, policy
        ) and cls._five_hour_reset_is_valid_or_missing(usage)

    def _priming_controller(
        self,
        *,
        slot: str,
        baseline: dict,
        policies: dict[str, SlotPolicy],
        shared: SharedProfileSettings,
        primed_slots: list[str],
        baseline_failures: int,
        controller_revision: str,
    ) -> dict:
        now = self.clock()
        return {
            "phase": "priming-pending",
            "activeSlot": slot,
            "identity": self.switcher.account_identity(slot),
            "policyRevision": self._policy_revision(policies, shared),
            "controllerRevision": controller_revision,
            "baselineFiveHourResetAt": self._five_hour_reset(baseline),
            "activatedAt": now,
            "failedPolls": 0,
            "lastFailedAttemptAt": None,
            "observedStoreFailures": baseline_failures,
            "escalated": False,
            **self._priming_progress(primed_slots),
        }

    def _tick_priming_pending(
        self,
        controller: dict,
        policies: dict[str, SlotPolicy],
        shared: SharedProfileSettings,
    ) -> TickOutcome:
        slot = controller.get("activeSlot")
        activated_at = controller.get("activatedAt")
        # A #10-era placeholder may exist without the #11 proof fields.
        # It is still a pin: preserve it without guessing a baseline or
        # silently converting it into ordinary rotation.
        if not isinstance(activated_at, (int, float)):
            self._emit(NoSwitchEvent(reason="priming-pending"))
            return TickOutcome.NO_ACTION
        if (
            not isinstance(slot, str)
            or slot not in policies
            or self.switcher.current_account_number() != slot
            or controller.get("identity") != self.switcher.account_identity(slot)
            or controller.get("policyRevision")
            != self._policy_revision(policies, shared)
        ):
            self._emit(
                NoSwitchEvent(
                    reason="priming-pin-invalid",
                    detail="retaining the current profile; operator reconciliation required",
                )
            )
            return TickOutcome.BLOCKED

        entries = self.switcher.usage_entries_by_account(fetch={slot})
        entry = entries.get(slot)
        if entry is None:
            self._emit(NoSwitchEvent(reason="priming-pending"))
            return TickOutcome.NO_ACTION

        successful_after_activation = (
            entry.last_error is None
            and entry.fetched_at is not None
            and entry.fetched_at > float(activated_at)
            and isinstance(entry.last_good, dict)
        )
        if successful_after_activation:
            fresh_reset = self._five_hour_reset(entry.last_good)
            baseline_reset = controller.get("baselineFiveHourResetAt")
            fresh_ts = _parse_reset_ts(fresh_reset)
            baseline_ts = _parse_reset_ts(baseline_reset)
            proved = fresh_ts is not None and (
                baseline_ts is None or fresh_ts > baseline_ts
            )
            if proved:
                primed = self._primed_slots(controller, policies)
                if slot not in primed:
                    primed.append(slot)
                    primed.sort(key=int)
                self._store_controller(
                    {
                        "phase": "priming",
                        **self._priming_progress(primed),
                    }
                )
                self._emit(
                    NoSwitchEvent(
                        reason="priming-confirmed",
                        detail=f"rotating seat {slot} opened a fresh 5-hour window",
                    )
                )
                return TickOutcome.NO_ACTION
            if (
                controller.get("failedPolls")
                or controller.get("observedStoreFailures")
                or controller.get("escalated")
            ):
                controller = {
                    **controller,
                    "failedPolls": 0,
                    "observedStoreFailures": 0,
                    "escalated": False,
                }
                self._store_controller(controller)
            self._emit(NoSwitchEvent(reason="priming-pending"))
            return TickOutcome.NO_ACTION

        attempted_after_activation = (
            entry.last_attempt_at is not None
            and entry.last_attempt_at > float(activated_at)
            and entry.last_error is not None
        )
        failures = int(controller.get("failedPolls", 0))
        observed_store_failures = int(
            controller.get("observedStoreFailures", 0)
        )
        new_store_failures = 0
        if attempted_after_activation:
            if entry.consecutive_failures > observed_store_failures:
                new_store_failures = (
                    entry.consecutive_failures - observed_store_failures
                )
            elif (
                entry.consecutive_failures > 0
                and entry.last_attempt_at
                != controller.get("lastFailedAttemptAt")
            ):
                # A success reset the store counter between controller ticks,
                # followed by a new failure.
                new_store_failures = entry.consecutive_failures
        failures += new_store_failures
        escalated = failures >= self.settings.unhealthy_ticks
        if (
            failures != controller.get("failedPolls")
            or escalated != controller.get("escalated")
            or (
                attempted_after_activation
                and entry.consecutive_failures != observed_store_failures
            )
        ):
            controller = {
                **controller,
                "failedPolls": failures,
                "lastFailedAttemptAt": (
                    entry.last_attempt_at
                    if new_store_failures
                    else controller.get("lastFailedAttemptAt")
                ),
                "observedStoreFailures": (
                    entry.consecutive_failures
                    if attempted_after_activation
                    else observed_store_failures
                ),
                "escalated": escalated,
            }
            self._store_controller(controller)
        if escalated:
            self._emit(
                ErrorEvent(
                    message=(
                        f"rotating seat {slot} priming proof poll failed "
                        f"{failures} consecutive times; profile remains pinned"
                    ),
                    transient=True,
                )
            )
            return TickOutcome.ERROR
        self._emit(NoSwitchEvent(reason="priming-pending"))
        return TickOutcome.NO_ACTION

    def _begin_priming(
        self,
        *,
        target: str,
        current: str,
        controller: dict | None,
        snapshot_usage: dict[str, dict],
        policies: dict[str, SlotPolicy],
        shared: SharedProfileSettings,
        primed_slots: list[str],
        baseline_failures: int,
        baseline_fetched_at: float | None,
        expected_identities: dict[str, dict],
        current_email: str,
    ) -> TickOutcome:
        baseline = snapshot_usage.get(target)
        if (
            baseline_fetched_at is None
            or not self._verified_priming_baseline(baseline, policies[target])
        ):
            self._emit(
                NoSwitchEvent(
                    reason="priming-baseline-unavailable",
                    detail=(
                        f"rotating seat {target} lacks fresh below-ceiling "
                        "usage proof"
                    ),
                )
            )
            return TickOutcome.BLOCKED
        assert isinstance(baseline, dict)
        prelock_baseline = baseline
        policy_revision = self._policy_revision(policies, shared)
        selection_epoch = hashlib.sha256(
            json.dumps(
                {
                    "trigger": "priming",
                    "targetSlot": target,
                    "policyRevision": policy_revision,
                    "baseline": prelock_baseline,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if self.dry_run:
            self._emit(
                NoSwitchEvent(
                    reason="priming-pending",
                    detail=(
                        f"would pin rotating seat {target}; "
                        "dry-run made no state change"
                    ),
                )
            )
            return TickOutcome.NO_ACTION

        result = None
        with self._state_lock():
            state = self._read_state()
            if state.get("sharedProfileController") != controller:
                self._emit(NoSwitchEvent(reason="controller-changed"))
                return TickOutcome.NO_ACTION
            locked_shared = load_shared_profile_settings(self.switcher.backup_dir)
            locked_policies = self._shared_policies(
                set(state.get("quarantine", {}))
                if isinstance(state.get("quarantine"), dict)
                else set()
            )
            if (
                not locked_shared.enabled
                or target not in locked_policies
                or self._policy_revision(locked_policies, locked_shared)
                != self._policy_revision(policies, shared)
                or {
                    slot: self.switcher.account_identity(slot)
                    for slot in locked_policies
                }
                != expected_identities
                or self.switcher.current_account_number() != current
            ):
                self._emit(NoSwitchEvent(reason="policy-changed"))
                return TickOutcome.NO_ACTION
            status = self._freshen_target(
                target, self.switcher.account_email(target)
            )
            if status != "ok":
                self._emit(NoSwitchEvent(reason="target-freshen-failed"))
                return TickOutcome.NO_ACTION
            locked_baseline = self.switcher.fetch_usage_now(target)
            locked_fetched_at = self.clock()
            if not self._verified_priming_baseline(
                locked_baseline, locked_policies[target]
            ):
                self._emit(NoSwitchEvent(reason="target-recheck-ineligible"))
                return TickOutcome.NO_ACTION
            assert isinstance(locked_baseline, dict)
            baseline = locked_baseline
            if target != current:
                result = self.switcher.switch_to(target, json_output=True)
                if not result or not result.get("switched"):
                    self._emit(NoSwitchEvent(reason="already-active"))
                    return TickOutcome.NO_ACTION
            state["schemaVersion"] = STATE_SCHEMA_VERSION
            state["sharedProfileController"] = self._priming_controller(
                slot=target,
                baseline=baseline,
                policies=locked_policies,
                shared=locked_shared,
                primed_slots=primed_slots,
                baseline_failures=baseline_failures,
                controller_revision=selection_epoch,
            )
            atomic_write_json(self.state_path, state)

        if result:
            self._emit_shared_activation_proof(
                trigger="priming",
                selection_epoch=selection_epoch,
                policy_revision=policy_revision,
                controller_state="priming-pending",
                target=target,
                prelock_usage=prelock_baseline,
                locked_usage=baseline,
                prelock_fetched_at=baseline_fetched_at,
                locked_fetched_at=locked_fetched_at,
                policy=locked_policies[target],
            )
            self._emit(
                SwitchEvent(
                    trigger="priming",
                    from_ref=result.get("from") or _ref(current, current_email),
                    to_ref=result.get("to"),
                    warnings=result.get("warnings", []),
                )
            )
        else:
            self._emit(NoSwitchEvent(reason="priming-pending"))
        return TickOutcome.SWITCHED if result else TickOutcome.NO_ACTION

    @staticmethod
    def _all_capped_recovery(
        usage: dict[str, dict],
        policies: dict[str, SlotPolicy],
        now: float,
    ) -> CappedRecovery:
        if not policies:
            return CappedRecovery(False)
        recoveries: list[float | None] = []
        for slot, policy in policies.items():
            value = usage.get(slot)
            if not isinstance(value, dict):
                return CappedRecovery(False)
            blocking_windows: list[dict] = []
            for key, ceiling in (
                ("five_hour", policy.five_hour_ceiling_pct),
                ("seven_day", policy.weekly_ceiling_pct),
            ):
                window = value.get(key)
                if not isinstance(window, dict):
                    return CappedRecovery(False)
                pct = window.get("pct")
                if (
                    isinstance(pct, bool)
                    or not isinstance(pct, (int, float))
                    or not math.isfinite(pct)
                ):
                    return CappedRecovery(False)
                if float(pct) >= ceiling:
                    blocking_windows.append(window)
            if not blocking_windows:
                return CappedRecovery(False)
            blocking_resets = [
                _parse_reset_ts(window.get("resets_at"))
                for window in blocking_windows
            ]
            if any(reset is None or reset <= now for reset in blocking_resets):
                recoveries.append(None)
            else:
                recoveries.append(
                    max(
                        reset
                        for reset in blocking_resets
                        if reset is not None
                    )
                )
        if any(item is None for item in recoveries):
            return CappedRecovery(True)
        return CappedRecovery(
            True,
            min(item for item in recoveries if item is not None)
            + RESET_SLACK_S,
        )

    def _tick_shared_profile(
        self,
        *,
        current: str,
        current_email: str,
        state: dict,
        shared: SharedProfileSettings,
        quarantined: set[str],
    ) -> TickOutcome:
        controller = state.get("sharedProfileController")
        policies = self._shared_policies(quarantined)
        if shared.rollout_stage == "canary" and len(policies) != 1:
            return self._hold_shared_profile(
                reason="canary-roster-scope",
                shared=shared,
                controller=controller,
                worker_ready=True,
                persist=True,
            )
        if (
            shared.rollout_stage == "small-roster"
            and any(
                policy.five_hour_ceiling_pct == 50.0
                for policy in policies.values()
            )
        ):
            return self._hold_shared_profile(
                reason="protected-seat-stage-required",
                shared=shared,
                controller=controller,
                worker_ready=True,
                persist=True,
            )
        if (
            isinstance(controller, dict)
            and controller.get("phase") == "priming-pending"
        ):
            return self._tick_priming_pending(controller, policies, shared)

        epoch_started = self.clock()
        entries = self.switcher.usage_entries_by_account(
            fetch=set(policies),
            force=True,
        )
        usage = {
            slot: entry.last_good
            for slot, entry in entries.items()
            if slot in policies
            and entry.fetched_at is not None
            and entry.fetched_at >= epoch_started
            and entry.last_error is None
            and isinstance(entry.last_good, dict)
        }
        fetched_at_by_slot = {
            slot: float(entry.fetched_at)
            for slot, entry in entries.items()
            if slot in usage and entry.fetched_at is not None
        }
        ranked = rank_paced_slots(usage, policies, self.clock())
        active_usage = usage.get(current)
        active_eligible = (
            current in policies
            and active_usage is not None
            and verified_eligible(active_usage, policies[current])
        )
        revision = self._policy_revision(policies, shared)
        identity = self.switcher.account_identity(current)
        identities = {
            slot: self.switcher.account_identity(slot) for slot in policies
        }
        primed_slots = self._primed_slots(controller, policies)
        unprimed = sorted(
            (slot for slot in policies if slot not in primed_slots),
            key=lambda slot: (-policies[slot].priority, int(slot)),
        )
        if unprimed:
            baseline_entry = entries.get(unprimed[0])
            return self._begin_priming(
                target=unprimed[0],
                current=current,
                controller=controller if isinstance(controller, dict) else None,
                snapshot_usage=usage,
                policies=policies,
                shared=shared,
                primed_slots=primed_slots,
                baseline_failures=(
                    baseline_entry.consecutive_failures
                    if baseline_entry is not None
                    else 0
                ),
                baseline_fetched_at=fetched_at_by_slot.get(unprimed[0]),
                expected_identities=identities,
                current_email=current_email,
            )

        snapshot_revision = hashlib.sha256(
            json.dumps(
                {
                    "epochStarted": epoch_started,
                    "activeSlot": current,
                    "policyRevision": revision,
                    "identities": identities,
                    "usage": usage,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        steady_valid = (
            isinstance(controller, dict)
            and controller.get("phase") == "steady"
            and controller.get("activeSlot") == current
            and controller.get("identity") == identity
            and controller.get("policyRevision") == revision
        )
        if not steady_valid:
            if active_eligible:
                assert active_usage is not None
                self._store_controller(
                    self._steady_controller(
                        current,
                        active_usage,
                        policies,
                        shared,
                        primed_slots=primed_slots,
                        controller_revision=snapshot_revision,
                    )
                )
                self._emit(NoSwitchEvent(reason="controller-baseline"))
                return TickOutcome.NO_ACTION

        target = next((slot for slot in ranked if slot != current), None)
        if target is None:
            if active_eligible:
                self._emit(NoSwitchEvent(reason="paced-active"))
                return TickOutcome.NO_ACTION
            recovery = self._all_capped_recovery(
                usage, policies, self.clock()
            )
            if recovery.all_capped:
                proposed_park_slot = min(
                    policies,
                    key=lambda slot: (-policies[slot].priority, int(slot)),
                )
                blocked = {
                    "phase": "verification-blocked",
                    "reason": "worker-admission-hold-unavailable",
                    "allCapped": True,
                    "activeSlot": current,
                    "proposedParkSlot": proposed_park_slot,
                    "wakeAt": recovery.wake_at,
                    "policyRevision": revision,
                    **self._priming_progress(primed_slots),
                }
                self._store_controller(blocked)
                self._emit(
                    NoSwitchEvent(
                        reason="worker-admission-hold-unavailable",
                        detail=(
                            "all rotating seats are freshly capped; retaining "
                            f"rotating seat {current} instead of parking on "
                            f"rotating seat {proposed_park_slot}"
                        ),
                    )
                )
                return TickOutcome.BLOCKED
            self._emit(NoSwitchEvent(reason="verification-blocked"))
            return TickOutcome.BLOCKED

        if active_eligible:
            if ranked and ranked[0] == current:
                self._emit(NoSwitchEvent(reason="paced-active"))
                return TickOutcome.NO_ACTION
            assert isinstance(controller, dict)
            dwell_until = controller.get("dwellUntil")
            if not isinstance(dwell_until, (int, float)) or self.clock() < dwell_until:
                self._emit(NoSwitchEvent(reason="dwell"))
                return TickOutcome.NO_ACTION
            if not material_usage_changed(
                controller.get("activationUsage"),
                active_usage,
                shared.material_usage_delta_pct,
            ):
                self._emit(NoSwitchEvent(reason="material-usage"))
                return TickOutcome.NO_ACTION

        return self._perform_shared_profile(
            target=target,
            expected_controller=controller,
            expected_current=current,
            expected_current_identity=self.switcher.account_identity(current),
            expected_identities=identities,
            snapshot_revision=snapshot_revision,
            snapshot_usage=usage,
            snapshot_fetched_at=fetched_at_by_slot[target],
            policies=policies,
            shared=shared,
            failover=not active_eligible,
            current_email=current_email,
            primed_slots=primed_slots,
        )

    def _perform_shared_profile(
        self,
        *,
        target: str,
        expected_controller: dict | None,
        expected_current: str,
        expected_current_identity: dict,
        expected_identities: dict[str, dict],
        snapshot_revision: str,
        snapshot_usage: dict[str, dict],
        snapshot_fetched_at: float,
        policies: dict[str, SlotPolicy],
        shared: SharedProfileSettings,
        failover: bool,
        current_email: str,
        primed_slots: list[str],
    ) -> TickOutcome:
        if self.dry_run:
            return self._perform(target, self.switcher.account_email(target), "paced")

        committed_failover = failover
        with self._state_lock():
            state = self._read_state()
            if state.get("sharedProfileController") != expected_controller:
                self._emit(NoSwitchEvent(reason="controller-changed"))
                return TickOutcome.NO_ACTION
            current = self.switcher.current_account_number()
            if current != expected_current or current == target:
                self._emit(NoSwitchEvent(reason="controller-changed"))
                return TickOutcome.NO_ACTION
            state["schemaVersion"] = STATE_SCHEMA_VERSION
            state["sharedProfileController"] = {
                "phase": "selecting",
                "reason": "failover" if failover else "paced",
                "snapshotRevision": snapshot_revision,
                "activeSlot": current,
                "targetSlot": target,
                "selectedAt": self.clock(),
            }
            atomic_write_json(self.state_path, state)

            def abort(reason: str) -> TickOutcome:
                if expected_controller is None:
                    state.pop("sharedProfileController", None)
                else:
                    state["sharedProfileController"] = expected_controller
                atomic_write_json(self.state_path, state)
                self._emit(NoSwitchEvent(reason=reason))
                return TickOutcome.NO_ACTION

            locked_shared = load_shared_profile_settings(self.switcher.backup_dir)
            locked_quarantine = state.get("quarantine")
            locked_policies = self._shared_policies(
                set(locked_quarantine)
                if isinstance(locked_quarantine, dict)
                else set()
            )
            if (
                not locked_shared.enabled
                or target not in locked_policies
                or self._policy_revision(locked_policies, locked_shared)
                != self._policy_revision(policies, shared)
                or {
                    slot: self.switcher.account_identity(slot)
                    for slot in locked_policies
                }
                != expected_identities
                or self.switcher.account_identity(current)
                != expected_current_identity
            ):
                return abort("policy-changed")
            status = self._freshen_target(
                target, self.switcher.account_email(target)
            )
            if status != "ok":
                return abort("target-freshen-failed")
            locked_active_usage = self.switcher.fetch_usage_now(current)
            locked_target_usage = self.switcher.fetch_usage_now(target)
            locked_fetched_at = self.clock()
            if not verified_eligible(
                locked_target_usage, locked_policies[target]
            ):
                return abort("target-recheck-ineligible")
            assert isinstance(locked_target_usage, dict)
            locked_usage_by_slot = dict(snapshot_usage)
            locked_usage_by_slot[current] = locked_active_usage
            locked_usage_by_slot[target] = locked_target_usage
            locked_ranked = rank_paced_slots(
                locked_usage_by_slot, locked_policies, self.clock()
            )
            if not locked_ranked or locked_ranked[0] != target:
                return abort("ranking-changed")
            locked_active_eligible = (
                current in locked_policies
                and verified_eligible(
                    locked_active_usage, locked_policies[current]
                )
            )
            committed_failover = not locked_active_eligible
            if locked_active_eligible:
                if not isinstance(expected_controller, dict):
                    return abort("controller-changed")
                dwell_until = expected_controller.get("dwellUntil")
                if (
                    not isinstance(dwell_until, (int, float))
                    or self.clock() < dwell_until
                ):
                    return abort("dwell")
                if not material_usage_changed(
                    expected_controller.get("activationUsage"),
                    locked_active_usage,
                    locked_shared.material_usage_delta_pct,
                ):
                    return abort("material-usage")
            result = self.switcher.switch_to(target, json_output=True)
            if not result or not result.get("switched"):
                return abort("already-active")
            state["schemaVersion"] = STATE_SCHEMA_VERSION
            state["lastSwitchAt"] = self.clock()
            state["lastSwitchTo"] = target
            state["sharedProfileController"] = self._steady_controller(
                target,
                locked_target_usage,
                locked_policies,
                locked_shared,
                primed_slots=primed_slots,
                controller_revision=snapshot_revision,
            )
            atomic_write_json(self.state_path, state)

        committed_trigger = "failover" if committed_failover else "paced"
        self._emit_shared_activation_proof(
            trigger=committed_trigger,
            selection_epoch=snapshot_revision,
            policy_revision=self._policy_revision(locked_policies, locked_shared),
            controller_state="steady",
            target=target,
            prelock_usage=snapshot_usage[target],
            locked_usage=locked_target_usage,
            prelock_fetched_at=snapshot_fetched_at,
            locked_fetched_at=locked_fetched_at,
            policy=locked_policies[target],
        )
        self._emit(
            SwitchEvent(
                trigger=committed_trigger,
                from_ref=result.get("from")
                or _ref(current, current_email),
                to_ref=result.get("to"),
                warnings=result.get("warnings", []),
            )
        )
        return TickOutcome.SWITCHED

    def _rank_candidates(
        self,
        *,
        trigger: str,
        consume_first: bool,
        oauth_candidates: list[str],
        usage: dict[str, dict | str | None],
        headroom: dict[str, float | None],
        current: str,
        active_headroom: float | None,
        settings: AutoSwitchSettings,
        now: float,
    ) -> tuple[list[str], bool, float | None]:
        """Filter and rank OAuth candidates for this tick's trigger.

        Returns ``(ordered, any_known, active_reset_ts)``. Pure — no emits,
        no state writes — so the consume-first two-phase commit can run it
        twice per tick: on the stored snapshot to decide provisionally, then
        on the escalated refetch to re-verify before switching.
        """
        # consume-first ranks by soonest weekly reset; a proactive (below-
        # threshold) target must reset strictly sooner than where we are.
        active_reset_ts = (
            _seven_day_reset_ts(usage.get(current), now) if consume_first else None
        )
        qualifying: list[tuple[tuple, str]] = []
        any_known = False
        for num in oauth_candidates:
            h = headroom.get(num)
            if h is None:
                continue
            any_known = True
            if h <= 0:
                continue  # itself at its limit — never a target
            reset_ts = (
                _seven_day_reset_ts(usage.get(num), now) if consume_first else None
            )
            cand_pcts = (
                _window_pcts(
                    usage.get(num) if isinstance(usage.get(num), dict) else None,
                    self._models,
                )
                if consume_first
                else {}
            )
            if trigger in ("proactive", "consume-first"):
                # Landing must be healthy: an account already over the trigger
                # would re-fire on the very next tick. best keeps the binding
                # (max-fold) gate; consume-first gates on the 5h window only
                # (staying under the 5h limit is the priority), so a 7d-heavy
                # but 5h-idle account is still a valid landing — its weekly load
                # only deprioritizes it in the ranking below. At-limit and
                # failover skip this block entirely (any live account beats a
                # blocked or dead one).
                if consume_first:
                    five_h = cand_pcts.get("5h")
                    if five_h is not None and five_h >= settings.eff_5h():
                        continue
                elif (100.0 - h) >= settings.threshold:
                    continue
                if consume_first:
                    # Purely proactive on reset ordering: below the threshold,
                    # only move to accounts whose weekly window resets sooner
                    # than the active one (above the threshold we must move, so
                    # any healthy account qualifies and the sort picks soonest).
                    if trigger == "consume-first" and (
                        reset_ts is None
                        or active_reset_ts is None
                        or reset_ts >= active_reset_ts
                    ):
                        continue
                elif active_headroom is not None:
                    # best: the candidate must beat the active account by the
                    # full hysteresis margin (a one-way move like 99%→89%
                    # qualifies; near-line pairs can't flap back).
                    if h - active_headroom < settings.hysteresis_pct:
                        continue
            if consume_first:
                # 7d-heavy targets (at/over the effective 7d threshold) sink
                # below lighter ones — deprioritize, not exclude, so 5h relief
                # still wins when they are the only option. Within a tier:
                # soonest weekly reset first (unknown resets sort last), most
                # headroom breaks ties, then sequence order.
                seven_d = cand_pcts.get("7d")
                heavy = 1 if (seven_d is not None and seven_d >= settings.eff_7d()) else 0
                key: tuple = (
                    heavy,
                    reset_ts if reset_ts is not None else float("inf"),
                    -h,
                )
            else:
                key = (-h,)
            qualifying.append((key, num))
        # Ascending by the strategy's key; list order (sequence order) breaks ties.
        qualifying.sort(key=lambda t: t[0])
        return [num for _, num in qualifying], any_known, active_reset_ts

    # -- adaptive usage scheduling ---------------------------------------------

    def _collect_scheduled_usage(
        self,
        current: str,
        quarantined: set[str] = frozenset(),
        *,
        threshold: float | None = None,
    ) -> tuple[dict, dict[str, dict | str | None], dict[str, float | None]]:
        """Two-phase usage collection with an O(1) baseline.

        Phase A fetches the active account (when its persisted poll plan says
        it is due — poll_policy's urgent mode is what tightens that cadence
        near the band) plus ONE due candidate (the one with the stalest data
        — never-fetched first, then oldest fetch); everyone else is served
        from the usage store. Phase B refetches ALL candidates and recomputes
        before any switch decision when a switch could be near: active
        utilization within ``ESCALATION_MARGIN_PCT`` of the threshold, or
        active usage unknown (failover must not run on stale candidate data).
        At-limit, proactive, and ordinary unknown-usage failover selection
        never runs on the pre-escalation snapshot — those triggers imply the
        escalation condition (the deliberate exception: an owned-and-expired
        active is excluded above, so a post-idle-hold failover can run
        without escalating). The consume-first trigger can fire outside the
        escalation band, so it instead decides *provisionally* on the stored
        snapshot and, only when a switch would fire, re-runs an escalated
        collection and re-verifies the choice in ``_tick_inner`` (two-phase
        commit), plus a per-target ``UsageEntry.fresh`` gate before
        performing.

        Stalest-first needs no rotation cursor: it reads the persisted store,
        so the loop and cron-driven ``--once`` runs schedule identically.
        Backoff (``backoffUntil``) is enforced by the collector even for the
        active account — a Retry-After must never be defeated — and during an
        idle-hold no candidate is polled at all (slow crawl for everything).
        Adapted cadences are persisted by the collector itself after each
        fetch (shared with every other surface), not by the engine.

        Returns ``(entries, usage, headroom)`` where ``usage`` carries
        decision values and ``headroom`` the derived headroom per account.
        """
        now = self.clock()
        # Quarantined accounts can never be switch targets, so spending the
        # single alternate poll slot (or an escalation fetch) on one is wasted.
        candidates = [
            n
            for n in self.switcher.switchable_account_numbers()
            if n != current and n not in quarantined
        ]

        pre = self.switcher.usage_entries_by_account(fetch=set())
        plan: set[str] = set()
        active_pre = pre.get(current)
        # The active account is nominated when never fetched, poll-due per its
        # persisted plan, or (no plan yet) past the normal cadence floor. The
        # collector's reserve() honors due-ness even inside the serve TTL, so
        # an urgent plan (60s while burning near the band) actually fetches.
        # A candidate-style plan (slower than any active plan can be) left
        # over from a role change the switcher never saw (e.g. a manual
        # login) is overridden past the active age cap. Exhausted accounts
        # carry their own bounded plan and become due normally.
        stale_candidate_plan = (
            active_pre is not None
            and active_pre.age_s is not None
            and active_pre.age_s >= poll_policy.ACTIVE_MAX_INTERVAL_S
            and (active_pre.poll_interval_s or 0.0)
            > poll_policy.ACTIVE_MAX_INTERVAL_S
            and (binding_pct(active_pre.last_good, self._models) or 0.0) < 100.0
        )
        overslept_plan = (
            active_pre is not None
            and plan_oversleeps_interval(active_pre, now)
        )
        if (
            active_pre is None
            or active_pre.age_s is None
            or stale_candidate_plan
            or overslept_plan
            or (
                active_pre.next_poll_at is not None
                and now >= active_pre.next_poll_at
            )
            or (
                active_pre.next_poll_at is None
                and active_pre.age_s >= poll_policy.MIN_INTERVAL_S
            )
        ):
            plan.add(current)
        if self._idle_hold_since is None:
            pick = due_candidate(candidates, pre, now)
            if pick is not None:
                plan.add(pick)
        entries = self.switcher.usage_entries_by_account(
            fetch=plan,
            # A candidate-style plan on the active slot is deliberately
            # overridden after the active age cap; every other baseline
            # nomination preserves a valid future plan under the store lock.
            scheduled=not stale_candidate_plan,
        )
        usage = {num: entry.decision_value() for num, entry in entries.items()}

        active_value = usage.get(current)
        active_headroom = oauth.account_headroom(
            active_value if isinstance(active_value, dict) else None, self._models
        )
        # The caller's tick-snapshotted threshold, so one tick fetches and
        # decides on the same value even if apply_threshold() lands mid-tick.
        if threshold is None:
            threshold = self.settings.min_effective_threshold()
        escalate = bool(candidates) and (
            (active_headroom is None and active_value != USAGE_TOKEN_EXPIRED)
            or (
                active_headroom is not None
                and 100.0 - active_headroom >= threshold - ESCALATION_MARGIN_PCT
            )
        )
        if escalate:
            escalation_fetch = {current, *candidates}
            # Escalation may beat ordinary candidate plans to obtain a fresh
            # switch decision, but a decision-trusted exhausted row cannot be
            # a target. Preserve any wider post-429 plan instead of refetching
            # that token at the bounded all-exhausted wake cadence.
            for num in tuple(escalation_fetch):
                entry = entries.get(num)
                value = usage.get(num)
                planned_headroom = oauth.account_headroom(
                    value if isinstance(value, dict) else None, self._models
                )
                if (
                    entry is not None
                    and entry.next_poll_at is not None
                    and now < entry.next_poll_at
                    and (entry.poll_interval_s or 0.0)
                    > poll_policy.EXHAUSTED_INTERVAL_S
                    and planned_headroom is not None
                    and planned_headroom <= 0
                ):
                    escalation_fetch.remove(num)
            entries = self.switcher.usage_entries_by_account(
                fetch=escalation_fetch
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}

        headroom = _headroom_by_account(usage, self._models)
        return entries, usage, headroom

    def _perform(self, number: str, email: str, trigger: str) -> TickOutcome:
        if self.dry_run:
            current = self.switcher.current_account_number()
            current_email = self.switcher.account_email(current) if current else ""
            self._emit(
                SwitchEvent(
                    trigger=trigger,
                    from_ref=_ref(current, current_email) if current else None,
                    to_ref=_ref(number, email),
                    dry_run=True,
                )
            )
            return TickOutcome.SWITCHED

        # Hold the state lock across the whole recheck -> switch -> record
        # sequence so two concurrent engines (loop + cron --once) make one
        # serialized decision: the loser re-reads the winner's lastSwitchAt
        # and backs off instead of double-switching. No deadlock cycle: the
        # switch path (cswap FileLock + Claude Code locks) never takes the
        # state lock.
        with self._state_lock():
            state = self._read_state()
            if trigger in ("proactive", "consume-first") and self._in_cooldown(state):
                self._emit(NoSwitchEvent(reason="cooldown"))
                return TickOutcome.NO_ACTION

            result = self.switcher.switch_to(number, json_output=True)
            if not result or not result.get("switched"):
                self._emit(
                    NoSwitchEvent(
                        reason="already-active",
                        detail=(result or {}).get("reason", ""),
                    )
                )
                return TickOutcome.NO_ACTION

            state["schemaVersion"] = STATE_SCHEMA_VERSION
            state["lastSwitchAt"] = self.clock()
            state["lastSwitchTo"] = number
            atomic_write_json(self.state_path, state)

        self._emit(
            SwitchEvent(
                trigger=trigger,
                from_ref=result.get("from"),
                to_ref=result.get("to"),
                warnings=result.get("warnings", []),
            )
        )
        return TickOutcome.SWITCHED

    # -- helpers --------------------------------------------------------------

    def _in_cooldown(self, state: dict) -> bool:
        last = state.get("lastSwitchAt")
        if not isinstance(last, (int, float)):
            return False
        return (self.clock() - last) < self.settings.cooldown_seconds

    def _check_model_names(
        self, quarantined: set[str], usage: dict[str, dict | str | None]
    ) -> None:
        """One-shot ``autoswitch.model`` typo guard.

        A configured name that no account reports means the filter looks
        active while gating nothing. That's only provable once every
        relevant oauth account has readable usage this tick — adaptive
        polling legitimately leaves gaps before that — and never worth a
        forced refresh of its own.
        """
        wanted = {m.lower(): m for m in self._models if m.lower() != "all"}
        if not wanted:
            self._model_check_done = True  # bare "all" needs no name match
            return
        relevant = [
            n
            for n in self.switcher.switchable_account_numbers()
            if n not in quarantined
            and self.switcher.account_kind_for(n) != "api_key"
        ]
        values = [usage.get(n) for n in relevant]
        readable = [v for v in values if isinstance(v, dict)]
        if not readable or len(readable) != len(values):
            return  # not every account observed yet — re-check next tick
        seen = {
            s["name"].lower()
            for v in readable
            for s in (v.get("scoped") or [])
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        }
        self._model_check_done = True
        missing = [name for low, name in wanted.items() if low not in seen]
        if missing:
            self._emit(
                ConfigWarningEvent(
                    message=(
                        f"autoswitch.model: {', '.join(missing)} matches no "
                        "account's usage windows — only the 5h/7d limits are "
                        "being watched for it (typo?)"
                    )
                )
            )

    def _earliest_recovery(
        self, usage: dict[str, dict | str | None]
    ) -> datetime | None:
        """Earliest moment any account becomes usable again (UTC), or None
        when that moment can't be proven.

        Per account that's the *latest* reset among its ≥100% relevant
        windows — an account blocked on both 5h and a scoped weekly limit
        isn't usable when the 5h rolls over — then the minimum across
        accounts, the active one included (its recovery also ends the
        blocked state). A blocked account whose exhausted windows carry no
        reset time at all could recover at any moment, so it makes the whole
        answer unprovable: return None and let the bounded blocked-cadence
        fallback re-check, rather than sleeping toward another account's
        later known reset."""
        earliest: float | None = None
        now = self.clock()
        for value in usage.values():
            if not isinstance(value, dict):
                continue
            blocked = [
                resets_at
                for _, pct, resets_at in oauth.relevant_windows(value, self._models)
                if pct >= 100.0
            ]
            if not blocked:
                continue  # not exhausted — doesn't gate the blocked state
            usable_at = _limiting_reset_ts(value, self._models)
            if usable_at is None or usable_at <= now:
                return None  # blocked with unprovable recovery — don't oversleep
            if earliest is None or usable_at < earliest:
                earliest = usable_at
        if earliest is None:
            return None
        return datetime.fromtimestamp(earliest, tz=timezone.utc)

    def _emit(self, event: AutoSwitchEvent) -> None:
        self.on_event(event)

    # -- loop -------------------------------------------------------------------

    def stop(self) -> None:
        """Ask ``run_loop`` to exit; wakes it from any sleep. Safe to call
        before the loop starts — the stop is never cleared, so the loop
        exits immediately (engines are single-use)."""
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Cut the current inter-tick sleep short and tick now."""
        self._wake.set()

    def apply_threshold(self, threshold: float) -> None:
        """Session override from the TUI: retarget the trigger and poll
        cadence mid-run. Threshold only — the model axes (and their derived
        state) are fixed at construction. The frozen-settings swap is atomic
        and each tick snapshots ``self.settings`` once, so no locking."""
        self.settings = replace(self.settings, threshold=threshold)
        self.switcher.set_poll_policy_inputs(threshold, self._models)

    def _next_delay(self, outcome: TickOutcome) -> float:
        interval = self.settings.interval_seconds
        if outcome is TickOutcome.BLOCKED:
            if self._sleep_until_ts is not None:
                delay = self._sleep_until_ts - self.clock()
                return min(max(delay, interval), MAX_SLEEP_S)
            if self._blocked_wait_long:
                # Truly exhausted with no reset time known / no candidates.
                return max(interval, NO_RESET_FALLBACK_S)
            # Blocked on something that can resolve any tick (hysteresis,
            # unreadable usage) — keep the normal cadence so the at-limit
            # escape isn't missed.
        elif outcome is TickOutcome.NO_ACTION and self._idle_hold_slow:
            # Idle-hold: Claude is idle on an expired token — nothing changes
            # until the user comes back, so crawl. Worst case protection
            # resumes one slow tick after they do.
            return max(interval, NO_RESET_FALLBACK_S)
        # ±10% jitter so multiple machines don't synchronize their API hits.
        return interval * (0.9 + 0.2 * random.random())

    def run_loop(self) -> int:
        """Tick forever (until :meth:`stop`); a failing tick never kills it."""
        while True:
            # Clear at the top, not after the wait: a wake() racing a wait
            # timeout is then never lost — the tick right after this clear
            # already sees whatever settings that wake announced.
            self._wake.clear()
            if self._stop.is_set():
                return 0
            try:
                outcome = self.tick()
            except Exception as e:  # pragma: no cover - tick() already guards
                self._emit(
                    ErrorEvent(message=f"{type(e).__name__}: {e}", transient=True)
                )
                outcome = TickOutcome.ERROR
            delay = self._next_delay(outcome)
            if delay > self.settings.interval_seconds * 1.5:
                until = datetime.now(timezone.utc) + timedelta(seconds=delay)
                self._emit(
                    SleepEvent(
                        seconds=delay,
                        until=until.isoformat(timespec="seconds").replace(
                            "+00:00", "Z"
                        ),
                    )
                )
            self._wake.wait(delay)
