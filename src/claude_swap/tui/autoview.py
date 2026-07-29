"""Live auto-switch screen: the real engine, visualized.

Runs :class:`AutoSwitchEngine` in a thread worker and renders its typed
events. Opens in **dry-run** — opening a view must never start switching
accounts on its own; going live is an explicit, confirmed action. The
engine's own state file semantics (shared cooldown, quarantine list, state
lock) make it safe to run alongside an external ``cswap auto``.

The active account's full card sits on top (same widget as the dashboard's
panel, with the threshold tick); this screen adds the engine badge, the
ranked switch candidates, and the decision log. While it is up, the app's
snapshot poller runs store-only: the engine is the only fetcher.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from claude_swap.autoswitch import (
    STATE_FILENAME,
    AutoSwitchEngine,
    AutoSwitchEvent,
    binding_pct,
    pct_label,
)
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.paced_selector import (
    material_usage_changed,
    rank_paced_slots,
    verified_eligible,
)
from claude_swap.poll_policy import parse_reset_ts
from claude_swap.settings import (
    DEFAULT_SLOT_POLICY,
    SETTING_SPECS,
    SharedProfileSettings,
    SlotPolicy,
    load_settings,
    load_shared_profile_settings,
    load_slot_policies,
    parse_model_names,
)
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import AccountsPanel

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_EVENT_ROLES = {
    "switch": "accent",
    "error": "sev_warn",
    "account-quarantined": "sev_warn",
    "all-exhausted": "sev_crit",
}
_QUIET_KINDS = {"poll", "no-switch", "sleep", "account-unquarantined"}


@dataclass(frozen=True)
class SharedProfileObservability:
    """Read-only terminal renderables for shared-profile controller proof."""

    enabled: bool
    policies: Text
    controller: Text


def shared_profile_observability(
    snap: AccountsSnapshot,
    backup_root: Path,
    *,
    now: float | None = None,
    unhealthy_ticks: int,
) -> SharedProfileObservability:
    """Render effective slot policy and proof facts without mutating either."""
    now = time.time() if now is None else now
    shared = load_shared_profile_settings(backup_root)
    explicit = load_slot_policies(backup_root)
    accounts = {int(account.number): account for account in snap.accounts}
    effective_policies = {
        account.number: explicit.get(int(account.number), DEFAULT_SLOT_POLICY)
        for account in snap.accounts
        if account.switchable and not account.disabled and account.kind != "api_key"
    }

    policies = Text("SLOT POLICIES  CLI writes · TUI observes\n", style="bold")
    policies.append(
        "slot  occupant                 usage / ceiling          source / priority  eligibility\n",
        style="dim",
    )
    for slot in sorted(set(accounts) | set(explicit)):
        account = accounts.get(slot)
        policy = explicit.get(slot, DEFAULT_SLOT_POLICY)
        source = (
            "explicit"
            if account is not None and slot in explicit
            else "defaulted"
            if account is not None
            else "vacant"
        )
        occupant = account.email if account is not None else "vacant"
        usage = (
            f"—/{policy.five_hour_ceiling_pct:g}% · "
            f"—/{policy.weekly_ceiling_pct:g}%"
        )
        eligibility = "vacant"
        if account is not None:
            entry = account.usage
            structural_failure = (
                "disabled"
                if account.disabled
                else "API key"
                if account.kind == "api_key"
                else "not switchable"
                if not account.switchable
                else None
            )
            fresh_usage = _fresh_usage(account, now)
            if fresh_usage is not None:
                five = data.window_pct(fresh_usage, "five_hour")
                weekly = data.window_pct(fresh_usage, "seven_day")
                if five is not None and weekly is not None:
                    usage = (
                        f"{five:g}/{policy.five_hour_ceiling_pct:g}% · "
                        f"{weekly:g}/{policy.weekly_ceiling_pct:g}%"
                    )
                if structural_failure is not None:
                    eligibility = f"ineligible · {structural_failure}"
                elif verified_eligible(fresh_usage, policy):
                    eligibility = "eligible · fresh"
                else:
                    capped = []
                    if five is not None and five >= policy.five_hour_ceiling_pct:
                        capped.append("5h capped")
                    if weekly is not None and weekly >= policy.weekly_ceiling_pct:
                        capped.append("weekly capped")
                    eligibility = (
                        "ineligible · " + ", ".join(capped)
                        if capped
                        else "ineligible · incomplete windows"
                    )
            else:
                if entry.sentinel is not None:
                    reason = data.sentinel_label(entry.sentinel)
                elif entry.last_error is not None:
                    reason = f"poll failed: {entry.last_error}"
                elif entry.age_s is not None and entry.last_good is not None:
                    reason = f"stale proof {data.format_duration(entry.age_s)}"
                else:
                    reason = "proof unavailable"
                reasons = (
                    f"{structural_failure}; {reason}"
                    if structural_failure is not None
                    else reason
                )
                eligibility = f"ineligible · {reasons}"
        policies.append(
            f"{slot:<5} {occupant:<24} {usage:<24} "
            f"{source} · pri {policy.priority:<4} {eligibility}\n"
        )

    try:
        raw_state = json.loads(
            (backup_root / STATE_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raw_state = {}
    controller_state = (
        raw_state.get("sharedProfileController")
        if isinstance(raw_state, dict)
        else None
    )
    controller = _controller_proof_text(
        controller_state,
        snap=snap,
        shared=shared,
        policies=effective_policies,
        now=now,
        unhealthy_ticks=unhealthy_ticks,
    )
    return SharedProfileObservability(shared.enabled, policies, controller)


def _controller_proof_text(
    state: object,
    *,
    snap: AccountsSnapshot,
    shared: SharedProfileSettings,
    policies: dict[str, SlotPolicy],
    now: float,
    unhealthy_ticks: int,
) -> Text:
    text = Text("CONTROLLER PROOF\n", style="bold")
    if not isinstance(state, dict):
        text.append("STATE   awaiting controller proof", style="dim")
        return text

    phase = str(state.get("phase") or "unknown")
    text.append(f"STATE   {phase}\n", style="cyan")
    if phase == "priming-pending":
        slot = state.get("activeSlot", "unknown")
        baseline = state.get("baselineFiveHourResetAt")
        text.append(f"PIN     slot {slot} · active profile remains pinned\n")
        text.append(
            "BASE    5h reset "
            f"{baseline if baseline is not None else 'unopened / unknown'}\n"
        )
        text.append(
            "PROOF   waiting for fresh post-activation 5h reset advance\n"
        )
        failures = int(state.get("failedPolls", 0))
        escalation = (
            "escalated · profile remains pinned"
            if state.get("escalated")
            else "below escalation threshold"
        )
        text.append(
            f"FAIL    {failures}/{unhealthy_ticks} proof polls · {escalation}"
        )
        return text

    primed = state.get("primedSlots")
    if isinstance(primed, list):
        label = ", ".join(str(slot) for slot in primed) or "none"
        text.append(f"PROOF   confirmed primed slots: {label}\n")
    active = state.get("activeSlot")
    if active is not None:
        text.append(f"ACTIVE  slot {active}\n")
    if phase == "steady":
        fresh_usage = {
            account.number: usage
            for account in snap.accounts
            if (usage := _fresh_usage(account, now)) is not None
            if account.number in policies
        }
        ranked = rank_paced_slots(fresh_usage, policies, now)
        paced = [
            slot
            for slot in ranked
            if (
                weekly_reset := parse_reset_ts(
                    fresh_usage[slot]["seven_day"].get("resets_at")
                )
            )
            is not None
            and weekly_reset > now
        ]
        fallback = [slot for slot in ranked if slot not in paced]
        if paced:
            text.append(
                f"RANK    {' > '.join(paced)} · slot {paced[0]} "
                "leads configured-maximum pace\n"
            )
        if fallback:
            text.append(
                f"FALLBACK {' > '.join(fallback)} · weekly reset unavailable; "
                "5h urgency then priority/slot\n"
            )
        if not ranked:
            text.append(
                "RANK    no verified eligible slot · fail-closed fallback\n"
            )
        dwell_until = state.get("dwellUntil")
        if isinstance(dwell_until, (int, float)):
            if dwell_until > now:
                text.append(
                    f"DWELL   {data.format_duration(dwell_until - now)} remaining "
                    "· voluntary move blocked\n"
                )
            else:
                text.append("DWELL   complete · voluntary move permitted\n")
        else:
            text.append(
                "DWELL   proof unavailable · voluntary move blocked\n"
            )

        account = next(
            (item for item in snap.accounts if item.number == str(active)),
            None,
        )
        current_usage = (
            _fresh_usage(account, now) if account is not None else None
        )
        baseline = state.get("activationUsage")
        if current_usage is None:
            text.append(
                "CHANGE  proof unavailable · active usage is not fresh"
            )
            return text
        changed = material_usage_changed(
            baseline, current_usage, shared.material_usage_delta_pct
        )
        delta = _largest_same_reset_delta(baseline, current_usage)
        if changed:
            text.append(
                f"CHANGE  confirmed · {delta:g}pp >= "
                f"{shared.material_usage_delta_pct:g}pp same-reset"
            )
        else:
            text.append(
                "CHANGE  waiting · need "
                f"{shared.material_usage_delta_pct:g}pp same-reset material usage"
            )
    elif phase == "selecting":
        text.append(
            f"SELECT  slot {state.get('targetSlot', 'unknown')} · "
            f"{state.get('reason', 'unknown')}\n"
        )
        text.append(f"SNAP    {state.get('snapshotRevision', 'unknown')}\n")
        text.append(
            "CHECK   fresh active + target; policy, identity, ranking"
        )
    elif phase == "verification-blocked":
        reason = str(state.get("reason") or "fresh all-slot proof incomplete")
        text.append(f"REASON  {reason.replace('-', ' ')}\n")
        if state.get("allCapped") is True:
            text.append("CAPS    all slots freshly proved capped\n")
        else:
            text.append("CAPS    not proved all-capped · bounded fresh poll\n")
        proposed = state.get("proposedParkSlot")
        if proposed is not None:
            text.append(
                f"PARK    proposed slot {proposed} · blocked; "
                f"retaining active slot {active}\n"
            )
        wake_at = state.get("wakeAt")
        if isinstance(wake_at, (int, float)):
            if wake_at > now:
                text.append(
                    f"WAKE    in {data.format_duration(wake_at - now)} · "
                    "earliest complete capped-window recovery"
                )
            else:
                text.append(
                    f"WAKE    overdue by {data.format_duration(now - wake_at)} "
                    "· recovery poll due now"
                )
        else:
            text.append(
                "WAKE    unknown · capped reset proof incomplete; bounded fresh poll"
            )
    return text


def _fresh_usage(account: AccountSnapshot, now: float) -> dict | None:
    entry = account.usage
    if (
        entry.sentinel is None
        and entry.last_error is None
        and entry.fresh(now)
        and isinstance(entry.last_good, dict)
    ):
        return entry.last_good
    return None


def _largest_same_reset_delta(baseline: object, fresh: object) -> float:
    if not isinstance(baseline, dict) or not isinstance(fresh, dict):
        return 0.0
    deltas = []
    for key in ("five_hour", "seven_day"):
        before = baseline.get(key)
        after = fresh.get(key)
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        before_pct = before.get("pct")
        after_pct = after.get("pct")
        if (
            isinstance(before_pct, (int, float))
            and not isinstance(before_pct, bool)
            and isinstance(after_pct, (int, float))
            and not isinstance(after_pct, bool)
            and before.get("resets_at") == after.get("resets_at")
        ):
            deltas.append(float(after_pct) - float(before_pct))
    return max(deltas, default=0.0)


def event_text(event: AutoSwitchEvent, *, palette: Palette = Palette.DARK) -> Text:
    """Log line for one engine event, styled like the CLI's human renderer."""
    role = _EVENT_ROLES.get(event.kind)
    if role is not None:
        style = getattr(palette, role)
    else:
        style = palette.muted if event.kind in _QUIET_KINDS else palette.foreground
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(event.human(), style=style)
    return text


class AutoScreen(Screen):
    BINDINGS = [
        Binding("l", "toggle_live", "Go live / dry-run"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("left", "threshold_step(-1)", "-1%"),
        Binding("right", "threshold_step(1)", "+1%"),
        Binding("enter", "adjust_done", "Done"),
        Binding("escape,q", "back", "Back"),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._engine: AutoSwitchEngine | None = None
        self._settings = None
        self._shared_profile = SharedProfileSettings()
        # Session-only threshold adjustment (t, then arrows). Never written
        # to settings.json — same memory-only precedent as the dry-run
        # toggle. ``_configured_threshold`` is the mount-time file value the
        # screen reverts to on exit; ``_entry_threshold`` is the value when
        # adjust mode was entered (wake/log only on a net change).
        self._adjusting = False
        self._configured_threshold: float | None = None
        self._entry_threshold: float | None = None

    def compose(self) -> ComposeResult:
        yield AccountsPanel(show_minis=False, id="auto-active-panel")
        with Vertical(id="auto-top"):
            with Horizontal(id="auto-title-row"):
                yield Static(" DRY-RUN ", id="mode-badge", classes="dry")
                yield Static("", id="auto-summary")
            yield Static("", id="candidates")
            with Horizontal(id="shared-observability"):
                yield Static("", id="slot-policies")
                yield Static("", id="controller-proof")
        yield RichLog(id="event-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        self.app.set_store_only(True)
        self._settings = load_settings(self.app.switcher.backup_dir)
        self._shared_profile = load_shared_profile_settings(
            self.app.switcher.backup_dir
        )
        # The bar tick everywhere reads app.threshold_pct, loaded once at app
        # startup — sync it to the fresh file value so bars and engine agree,
        # and remember that value: unmount restores it (only the session
        # adjustment reverts, not this correction).
        self._configured_threshold = self._settings.threshold
        self.app.threshold_pct = self._settings.threshold
        self._update_summary()
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "theme", self._on_theme_change)
        self._start_engine(dry_run=True)

    def on_unmount(self) -> None:
        if self._engine is not None:
            self._engine.stop()
        # A session threshold must not outlive the engine it steered: unpin
        # the poll planner and put the bar tick back on the file value.
        self.app.switcher.clear_poll_policy_inputs()
        if self._configured_threshold is not None:
            self.app.threshold_pct = self._configured_threshold
        self.app.set_store_only(False)

    def _on_theme_change(self, _theme: str) -> None:
        self._update_summary()
        self._update_badge()
        snap = self.app.snapshot
        if snap is not None:
            self._on_snapshot(snap)

    def action_back(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self.app.pop_screen()

    # -- threshold adjust mode ------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("threshold_step", "adjust_done") and not self._adjusting:
            return False  # hidden and inert until adjust mode is armed
        return True

    def action_adjust_threshold(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self._adjusting = True
        self._entry_threshold = self._settings.threshold
        self._update_summary()
        self.refresh_bindings()

    def action_adjust_done(self) -> None:
        if self._adjusting:
            self._end_adjust()

    def action_threshold_step(self, delta: float) -> None:
        if not self._adjusting:
            return
        spec = SETTING_SPECS["autoswitch.threshold"]
        value = min(spec.hi, max(spec.lo, self._settings.threshold + delta))
        self._set_threshold(value)

    def _end_adjust(self) -> None:
        self._adjusting = False
        self._update_summary()
        self.refresh_bindings()
        if self._settings.threshold == self._entry_threshold:
            return  # no net change: nothing to announce, no tick to force
        if self._engine is not None:
            self._engine.wake()  # show a decision at the new value now
        self.query_one("#event-log", RichLog).write(
            Text(
                f"— threshold set to {pct_label(self._settings.threshold)}% "
                "for this session —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _set_threshold(self, value: float) -> None:
        if value == self._settings.threshold:
            return
        self._settings = replace(self._settings, threshold=value)
        if self._engine is not None:
            self._engine.apply_threshold(value)
        self.app.threshold_pct = value
        self.query_one("#auto-active-panel", AccountsPanel).refresh()
        self._update_summary()

    def _update_summary(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        text = Text()
        if self._shared_profile.enabled:
            text.append("shared-profile rotation · read-only policy + proof")
            text.append(
                f" · threshold {pct_label(self._settings.threshold)}%",
                style=palette.accent if self._adjusting else palette.muted,
            )
            if self._settings.threshold != self._configured_threshold:
                text.append(" (session)", style=palette.muted)
            text.append(
                f" · poll every {self._settings.interval_seconds:.0f}s",
                style=palette.muted,
            )
            if self._adjusting:
                text.append("   ← → adjust · enter done", style=palette.muted)
            self.query_one("#auto-summary", Static).update(text)
            return
        text.append("auto-switch · ")
        text.append(
            f"threshold {pct_label(self._settings.threshold)}%",
            style=palette.accent if self._adjusting else "",
        )
        if self._settings.threshold != self._configured_threshold:
            text.append(" (session)", style=palette.muted)
        text.append(f" · poll every {self._settings.interval_seconds:.0f}s")
        if self._adjusting:
            text.append("   ← → adjust · enter done", style=palette.muted)
        self.query_one("#auto-summary", Static).update(text)

    # -- engine -------------------------------------------------------------

    def _start_engine(self, *, dry_run: bool) -> None:
        engine = AutoSwitchEngine(
            self.app.switcher,
            self._settings,
            self._emit_from_thread,
            dry_run=dry_run,
        )
        self._engine = engine
        self.run_worker(
            engine.run_loop,
            thread=True,
            group="engine",
            exit_on_error=False,
            name=f"auto-engine-{'dry' if dry_run else 'live'}",
        )
        self._update_badge()
        log = self.query_one("#event-log", RichLog)
        mode = "DRY-RUN (watching only)" if dry_run else "LIVE (will switch accounts)"
        log.write(
            Text(
                f"— engine started: {mode} —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _emit_from_thread(self, event: AutoSwitchEvent) -> None:
        """Engine ``on_event`` callback — runs on the worker thread."""
        try:
            self.app.call_from_thread(self._on_engine_event, event)
        except Exception:
            # App/screen tearing down mid-tick; the event has nowhere to go.
            pass

    def _on_engine_event(self, event: AutoSwitchEvent) -> None:
        if not self.is_attached:
            return
        palette = Palette.from_theme(self.app.current_theme)
        self.query_one("#event-log", RichLog).write(event_text(event, palette=palette))
        if self._shared_profile.enabled and self.app.snapshot is not None:
            self._update_shared_observability(self.app.snapshot)
        if event.kind == "switch":
            self.app.request_refresh()

    def action_toggle_live(self) -> None:
        if self._engine is None:
            return
        if self._engine.dry_run:
            self.app.push_screen(
                ConfirmModal(
                    "Go live? claude-swap will switch your active account "
                    "automatically when the threshold is reached.\n\n"
                    "(Same behavior as running `cswap auto` in a terminal.)",
                    title="Go live",
                    yes_label="Go live",
                ),
                self._on_live_confirm,
            )
        else:
            self._restart_engine(dry_run=True)

    def _on_live_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._restart_engine(dry_run=False)

    def _restart_engine(self, *, dry_run: bool) -> None:
        if self._engine is not None:
            self._engine.stop()
        self._start_engine(dry_run=dry_run)

    def _update_badge(self) -> None:
        badge = self.query_one("#mode-badge", Static)
        if self._engine is not None and not self._engine.dry_run:
            badge.update(" LIVE ")
            badge.set_classes("live")
        else:
            badge.update(" DRY-RUN ")
            badge.set_classes("dry")

    # -- candidates -----------------------------------------------------------

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        if self._shared_profile.enabled:
            self._update_shared_observability(snap)
            return
        self.query_one("#shared-observability").display = False
        self.query_one("#candidates", Static).display = True
        self.query_one("#candidates", Static).update(
            self._candidates_text(snap, active_number=snap.active_number)
        )

    def _update_shared_observability(self, snap: AccountsSnapshot) -> None:
        view = shared_profile_observability(
            snap,
            self.app.switcher.backup_dir,
            unhealthy_ticks=self._settings.unhealthy_ticks,
        )
        self.query_one("#candidates", Static).display = False
        self.query_one("#shared-observability").display = True
        self.query_one("#slot-policies", Static).update(view.policies)
        self.query_one("#controller-proof", Static).update(view.controller)

    def _candidates_text(
        self, snap: AccountsSnapshot, active_number: str | None
    ) -> Text:
        """Switch targets ranked by remaining headroom (best first)."""
        # Same window set as the engine (autoswitch.model included), so the
        # displayed ranking can never disagree with the account it picks.
        palette = Palette.from_theme(self.app.current_theme)
        models = parse_model_names(self._settings.model) if self._settings else ()
        ranked: list[tuple[float, str]] = []  # (sort key: pct used, number)
        lines: dict[str, Text] = {}
        for acc in snap.accounts:
            if acc.number == active_number or not acc.switchable:
                continue
            pct = binding_pct(acc.usage.last_good, models)
            entry = Text()
            entry.append(f"\n  {acc.number:>2}  ", style=palette.foreground)
            entry.append(acc.email, style=palette.foreground)
            if acc.usage.sentinel is not None:
                entry.append(
                    f"  {data.sentinel_label(acc.usage.sentinel)}", style=palette.muted
                )
                ranked.append((998.0, acc.number))
            elif pct is None:
                entry.append("  usage unknown", style=palette.muted)
                ranked.append((999.0, acc.number))
            else:
                entry.append(f"  {pct:3.0f}% used", style=palette.severity(pct))
                ranked.append((pct, acc.number))
            lines[acc.number] = entry

        text = Text()
        text.append("Next best", style=palette.muted)
        if not ranked:
            text.append("\n  no other switchable accounts", style=palette.muted)
            return text
        for _pct, number in sorted(ranked):
            text.append(lines[number])
        return text
