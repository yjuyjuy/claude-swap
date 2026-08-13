"""Tool settings persisted at ``<backup_root>/settings.json``.

One versioned JSON file for user-tunable claude-swap preferences, written
atomically with the backup dir's 0600/0700 modes. v1 carries the
``autoswitch`` and ``ui`` sections; other sections can be added additively.
Unknown keys (future fields, other tools' experiments) survive a round trip.

Reading is forgiving — a missing or corrupt file yields defaults with a logged
warning, never a crash — so a bad hand edit degrades to default behavior.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from claude_swap.exceptions import ConfigError
from claude_swap.fsutil import replace_with_retry
from claude_swap.locking import FileLock

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"
SETTINGS_LOCK_FILENAME = ".settings.lock"

_logger = logging.getLogger("claude-swap")


@dataclass(frozen=True)
class AutoSwitchSettings:
    """Policy knobs for the auto-switch engine (``cswap auto``).

    ``threshold`` is binding-window utilization (max of the 5h/7d percentages):
    at or above it the engine looks for a better account. 90 rather than 95
    leaves margin for the macOS ~30s Keychain pickup tail and for heavy
    subagent turns burning past the mark before a swap lands. A proactive
    candidate must itself sit below the threshold (never land somewhere that
    re-triggers next tick) and beat the active account's utilization by at
    least ``hysteresis_pct``, so two accounts hovering at the line never
    ping-pong while a strictly better account is always taken.

    ``five_hour_threshold`` / ``seven_day_threshold`` are optional per-window
    overrides. When set, the engine triggers on that window at its own
    threshold instead of the shared ``threshold``; when None (default) the
    window falls back to ``threshold`` (so leaving both unset reproduces the
    plain binding-window behavior exactly). Read them through ``eff_5h`` /
    ``eff_7d`` rather than the raw fields.
    """

    threshold: float = 90.0
    interval_seconds: float = 60.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0
    strategy: str = "best"  # "best" (most headroom) or "consume-first" (soonest weekly reset)
    include_api_key_accounts: bool = False
    unhealthy_ticks: int = 3
    # Comma-separated model display name(s) (e.g. "Fable" or "Fable,Opus"),
    # or "all" for every scoped window an account reports. Each named model's
    # per-model weekly limit is folded into the binding window, so the engine
    # switches off an account whose model quota is exhausted even while its
    # 5h/7d windows still have headroom. None = account-wide 5h/7d only
    # (default).
    model: str | None = None
    # Optional per-window trigger thresholds; None → fall back to ``threshold``.
    five_hour_threshold: float | None = None
    seven_day_threshold: float | None = None

    def eff_5h(self) -> float:
        """Effective 5-hour trigger threshold (override or shared fallback)."""
        return (
            self.five_hour_threshold
            if self.five_hour_threshold is not None
            else self.threshold
        )

    def eff_7d(self) -> float:
        """Effective 7-day trigger threshold (override or shared fallback)."""
        return (
            self.seven_day_threshold
            if self.seven_day_threshold is not None
            else self.threshold
        )

    def min_effective_threshold(self) -> float:
        """Lowest effective trigger threshold across every window.

        The usage-fetch escalation band keys off this so a per-window override
        that lowers a threshold still escalates candidate refetches in time
        for the switch it will cause.
        """
        return min(self.eff_5h(), self.eff_7d(), self.threshold)


@dataclass(frozen=True)
class UiSettings:
    """Appearance preferences (``ui`` section). ``theme`` selects the TUI/CLI
    color theme; ``auto`` follows terminal-background detection."""

    theme: str = "auto"


@dataclass(frozen=True)
class SharedProfileSettings:
    """Feature gate and global guards for shared-profile rotation.

    Policy configuration is safe to prepare before activation. Merely adding a
    slot policy must never enable shared-profile rotation, so the gate defaults
    off and is loaded independently from the forgiving classic settings.
    """

    enabled: bool = False
    rollout_stage: str = "contract"
    manual_hold: bool = False
    dwell_seconds: int = 900
    material_usage_delta_pct: float = 1.0


@dataclass(frozen=True)
class SlotPolicy:
    """Effective safety policy for one canonical current slot number."""

    five_hour_ceiling_pct: float = 90.0
    weekly_ceiling_pct: float = 100.0
    priority: int = 0


DEFAULT_SLOT_POLICY = SlotPolicy()


_SECTION_DEFAULT_SOURCES = {
    "autoswitch": AutoSwitchSettings,
    "autoswitch.sharedProfile": SharedProfileSettings,
    "ui": UiSettings,
}


@dataclass(frozen=True)
class SettingSpec:
    """Metadata for one user-tunable settings.json key.

    Single source of truth for bounds/choices: both the lenient clamp on load
    (`_clamped`) and the strict validation in `cswap config set`
    (`parse_setting_value`) read from here, so the two can't drift.
    """

    section: str  # dotted JSON object path ("autoswitch", "ui", ...)
    json_key: str  # camelCase key inside the section
    field: str  # snake_case AutoSwitchSettings field
    kind: str  # "float" | "int" | "bool" | "choice"
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.json_key}"

    @property
    def default(self):
        return getattr(_SECTION_DEFAULT_SOURCES[self.section](), self.field)


# settings.json uses camelCase (matching the repo's other JSON artifacts);
# dataclass fields stay snake_case.
SETTING_SPECS: dict[str, SettingSpec] = {
    spec.dotted: spec
    for spec in (
        SettingSpec(
            "autoswitch", "threshold", "threshold", "float", 50.0, 99.9,
            help="Switch when the binding 5h/7d window reaches this pct",
        ),
        SettingSpec(
            "autoswitch", "fiveHourThreshold", "five_hour_threshold", "float",
            50.0, 99.9,
            help="Per-window 5h trigger pct (unset falls back to threshold)",
        ),
        SettingSpec(
            "autoswitch", "sevenDayThreshold", "seven_day_threshold", "float",
            50.0, 99.9,
            help="Per-window 7d trigger pct (unset falls back to threshold)",
        ),
        SettingSpec(
            "autoswitch", "intervalSeconds", "interval_seconds", "float", 15.0, 3600.0,
            help="Poll interval for the cswap auto loop, in seconds",
        ),
        SettingSpec(
            "autoswitch", "cooldownSeconds", "cooldown_seconds", "float", 0.0, 86400.0,
            help="Minimum seconds between proactive switches",
        ),
        SettingSpec(
            "autoswitch", "hysteresisPct", "hysteresis_pct", "float", 0.0, 50.0,
            help="A target must beat the active account by this many pct",
        ),
        SettingSpec(
            "autoswitch", "strategy", "strategy", "choice",
            choices=("best", "consume-first"),
            help="How auto-switch picks the target account",
        ),
        SettingSpec(
            "autoswitch", "includeApiKeyAccounts", "include_api_key_accounts", "bool",
            help="Allow rotating onto managed API-key accounts (bill per token)",
        ),
        SettingSpec(
            "autoswitch", "unhealthyTicks", "unhealthy_ticks", "int", 1, 100,
            help="Consecutive failed polls before an account is unhealthy",
        ),
        SettingSpec(
            "autoswitch", "model", "model", "string",
            help="Also switch on these models' weekly limits (e.g. Fable, Fable,Opus, or all)",
        ),
        SettingSpec(
            "autoswitch.sharedProfile", "enabled", "enabled", "bool",
            help="Enable fail-closed shared-profile rotation",
        ),
        SettingSpec(
            "autoswitch.sharedProfile",
            "rolloutStage",
            "rollout_stage",
            "choice",
            choices=(
                "contract",
                "shadow",
                "canary",
                "small-roster",
                "protected-seat",
            ),
            help="Staged shared-profile release gate",
        ),
        SettingSpec(
            "autoswitch.sharedProfile",
            "manualHold",
            "manual_hold",
            "bool",
            help="Hold all autoswitch actuation during manual rollback",
        ),
        SettingSpec(
            "autoswitch.sharedProfile", "dwellSeconds", "dwell_seconds", "int",
            300, 3600,
            help="Minimum residence time for paced voluntary switches",
        ),
        SettingSpec(
            "autoswitch.sharedProfile",
            "materialUsageDeltaPct",
            "material_usage_delta_pct",
            "float",
            0.5,
            10.0,
            help="Same-reset raw usage increase required for a paced switch",
        ),
        SettingSpec(
            "ui", "theme", "theme", "choice", choices=("dark", "light", "auto"),
            help="Color theme; auto follows the terminal background",
        ),
    )
}

_AUTOSWITCH_KEYS: dict[str, str] = {
    spec.field: spec.json_key
    for spec in SETTING_SPECS.values()
    if spec.section == "autoswitch"
}


def settings_path(backup_root: Path) -> Path:
    return backup_root / SETTINGS_FILENAME


def settings_lock_path(backup_root: Path) -> Path:
    return backup_root / SETTINGS_LOCK_FILENAME


def parse_model_names(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated model list, trimmed and case-insensitively
    deduped (first spelling wins). Shared by the auto engine and the manual
    switch strategies so both read ``autoswitch.model`` identically."""
    if not value:
        return ()
    seen: dict[str, str] = {}
    for part in value.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return tuple(seen.values())


def _clamped(settings: AutoSwitchSettings) -> AutoSwitchSettings:
    """Clamp values into the SETTING_SPECS ranges; bad types → the default."""

    def num(value, default: float, lo: float, hi: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(min(max(value, lo), hi))

    kwargs = {}
    for spec in SETTING_SPECS.values():
        if spec.section != "autoswitch":
            continue
        value = getattr(settings, spec.field)
        if spec.kind in ("float", "int"):
            clamped = num(value, spec.default, spec.lo, spec.hi)
            kwargs[spec.field] = int(clamped) if spec.kind == "int" else clamped
        elif spec.kind == "bool":
            kwargs[spec.field] = bool(value)
        elif spec.kind == "string":
            # A non-empty string keeps as-is; anything else reverts to default
            # (None) so a null/garbage settings.json value disables the filter.
            kwargs[spec.field] = value if isinstance(value, str) and value else spec.default
        else:  # choice
            if value not in spec.choices:
                _logger.warning(
                    "settings.json: unsupported %s %r; using %r",
                    spec.dotted, value, spec.default,
                )
                value = spec.default
            kwargs[spec.field] = value
    return AutoSwitchSettings(**kwargs)


def _read_raw(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _logger.warning("Could not read %s (%s); using defaults", path, e)
        return {}
    if not isinstance(raw, dict):
        _logger.warning("%s is not a JSON object; using defaults", path)
        return {}
    return raw


def load_settings(backup_root: Path) -> AutoSwitchSettings:
    """Load the autoswitch section; missing/corrupt file or fields → defaults."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        return AutoSwitchSettings()
    kwargs = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        if json_key in section:
            kwargs[field] = section[json_key]
    try:
        settings = AutoSwitchSettings(**kwargs)
    except TypeError:
        settings = AutoSwitchSettings()
    return _clamped(settings)


def load_ui_settings(backup_root: Path) -> UiSettings:
    """Load the ui section; missing/corrupt file or unknown theme → default."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("ui")
    default = UiSettings()
    if not isinstance(section, dict):
        return default
    theme = section.get("theme", default.theme)
    if theme not in SETTING_SPECS["ui.theme"].choices:
        _logger.warning(
            "settings.json: unsupported ui.theme %r; using %r",
            theme, default.theme,
        )
        return default
    return UiSettings(theme=theme)


def save_settings(backup_root: Path, settings: AutoSwitchSettings) -> None:
    """Write the autoswitch section, preserving unknown keys and sections."""
    path = settings_path(backup_root)
    with FileLock(settings_lock_path(backup_root)):
        raw = _read_raw_for_write(path)
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        section = raw.get("autoswitch")
        if section is None:
            section = {}
        elif not isinstance(section, dict):
            raise ConfigError("settings.json autoswitch must be a JSON object")
        for field, json_key in _AUTOSWITCH_KEYS.items():
            section[json_key] = getattr(settings, field)
        raw["autoswitch"] = section
        atomic_write_json(path, raw)


def setting_spec(dotted_key: str) -> SettingSpec:
    """Look up a spec by dotted key; unknown keys raise with the valid list."""
    spec = SETTING_SPECS.get(dotted_key)
    if spec is None:
        raise ConfigError(
            f"unknown setting '{dotted_key}'\n"
            f"Valid keys: {', '.join(SETTING_SPECS)}"
        )
    return spec


_BOOL_WORDS = {
    "true": True, "1": True, "yes": True,
    "false": False, "0": False, "no": False,
}


def parse_setting_value(spec: SettingSpec, raw_value: str):
    """Strictly parse a CLI-provided string for `cswap config set`.

    Unlike the forgiving clamp on load, out-of-range or mistyped values raise
    ConfigError so the user learns about the problem when setting the value,
    not by silently degraded behavior at `cswap auto` time.
    """
    if spec.kind == "bool":
        # Never bool(str): bool("false") is True.
        parsed = _BOOL_WORDS.get(raw_value.strip().lower())
        if parsed is None:
            raise ConfigError(
                f"{spec.dotted} expects true or false (or 1/0, yes/no), "
                f"got '{raw_value}'"
            )
        return parsed
    if spec.kind == "choice":
        if raw_value not in spec.choices:
            raise ConfigError(
                f"{spec.dotted} must be one of: {', '.join(spec.choices)}"
            )
        return raw_value
    if spec.kind == "string":
        value = raw_value.strip()
        if not value:
            raise ConfigError(
                f"{spec.dotted} expects a non-empty value; use "
                f"'cswap config unset {spec.dotted}' to clear it"
            )
        return value
    try:
        value = int(raw_value) if spec.kind == "int" else float(raw_value)
    except ValueError:
        noun = "an integer" if spec.kind == "int" else "a number"
        raise ConfigError(
            f"{spec.dotted} expects {noun}, got '{raw_value}'"
        ) from None
    if not spec.lo <= value <= spec.hi:
        raise ConfigError(
            f"{spec.dotted} must be between {format_setting_value(spec.lo)} "
            f"and {format_setting_value(spec.hi)}"
        )
    return value


def format_setting_value(value) -> str:
    """Render a settings value the way settings.json writes it."""
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_raw_for_write(path: Path) -> dict:
    """Raw read for the config write path: a corrupt file errors, never {}.

    ``_read_raw``'s degrade-to-defaults is right for reads, but a
    read-modify-write starting from ``{}`` would replace a malformed (and
    maybe hand-recoverable) file with a near-empty one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {path}: {e}") from e

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConfigError(
                    f"{path} contains duplicate JSON key {key!r}; fix it before "
                    "changing or activating shared-profile settings"
                )
            result[key] = value
        return result

    try:
        raw = json.loads(text, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path} is not valid JSON ({e}); fix or delete it before "
            "changing settings"
        ) from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} is not a JSON object; fix or delete it before "
            "changing settings"
        )
    return raw


def _nested_section(
    raw: dict,
    dotted_path: str,
    *,
    create: bool,
    strict: bool = True,
) -> tuple[dict | None, list[tuple[dict, str]]]:
    """Resolve a dotted object path and retain parents for empty cleanup."""
    section = raw
    parents: list[tuple[dict, str]] = []
    for component in dotted_path.split("."):
        child = section.get(component)
        if child is None:
            if not create:
                return None, parents
            child = {}
            section[component] = child
        elif not isinstance(child, dict):
            if strict:
                raise ConfigError(
                    f"settings.json {dotted_path} must be a JSON object"
                )
            return None, parents
        parents.append((section, component))
        section = child
    return section, parents


def set_setting(backup_root: Path, dotted_key: str, raw_value: str):
    """Validate and persist one key for `cswap config set`; returns the value.

    Writes only the given key (plus schemaVersion) — deliberately not
    ``save_settings``, which writes every known key and would freeze the
    current defaults into the file, pinning users to them if a later version
    changes a default. Unknown keys and sections in the file survive.
    """
    spec = setting_spec(dotted_key)
    value = parse_setting_value(spec, raw_value)
    path = settings_path(backup_root)
    with FileLock(settings_lock_path(backup_root)):
        raw = _read_raw_for_write(path)
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        section, _ = _nested_section(raw, spec.section, create=True)
        assert section is not None
        section[spec.json_key] = value
        atomic_write_json(path, raw)
    return value


def unset_setting(backup_root: Path, dotted_key: str) -> bool:
    """Remove one key from settings.json; False if it wasn't set (no write)."""
    spec = setting_spec(dotted_key)
    path = settings_path(backup_root)
    with FileLock(settings_lock_path(backup_root)):
        raw = _read_raw_for_write(path)
        section, parents = _nested_section(raw, spec.section, create=False)
        if section is None or spec.json_key not in section:
            return False
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        del section[spec.json_key]
        for parent, component in reversed(parents):
            child = parent.get(component)
            if isinstance(child, dict) and not child:
                del parent[component]
            else:
                break
        atomic_write_json(path, raw)
        return True


def _slot_number(raw_slot: object, backup_root: Path) -> int:
    """Validate a canonical current slot key, including the legacy high-slot case."""
    if isinstance(raw_slot, bool):
        raise ConfigError("autoswitch.slotPolicies slot must be a canonical number")
    if isinstance(raw_slot, int):
        text = str(raw_slot)
    elif isinstance(raw_slot, str):
        text = raw_slot
    else:
        raise ConfigError("autoswitch.slotPolicies slot must be a canonical number")
    if not text.isascii() or not text.isdecimal() or str(int(text)) != text:
        raise ConfigError(
            f"autoswitch.slotPolicies key {text!r} is not a canonical positive "
            "decimal slot number"
        )
    slot = int(text)
    if slot < 1:
        raise ConfigError("autoswitch.slotPolicies slot must be between 1 and 99")
    if slot <= 99:
        return slot

    # Existing installations may already carry a historical slot above the
    # current 99-slot creation cap. It remains configurable, but the policy
    # writer cannot create a new out-of-range slot namespace.
    try:
        sequence = json.loads(
            (backup_root / "sequence.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        sequence = {}
    accounts = sequence.get("accounts") if isinstance(sequence, dict) else None
    if isinstance(accounts, dict) and text in accounts:
        return slot
    raise ConfigError(
        f"autoswitch.slotPolicies slot {slot} is out of range (1-99)"
    )


def _finite_percentage(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ConfigError(
            f"autoswitch.slotPolicies {field} must be a finite number "
            "between 1 and 100"
        )
    result = float(value)
    if not 1 <= result <= 100:
        raise ConfigError(
            f"autoswitch.slotPolicies {field} must be between 1 and 100"
        )
    return result


def _policy_priority(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            "autoswitch.slotPolicies priority must be an integer between "
            "-1000 and 1000"
        )
    if not -1000 <= value <= 1000:
        raise ConfigError(
            "autoswitch.slotPolicies priority must be between -1000 and 1000"
        )
    return value


def _parse_slot_policy(raw: object) -> SlotPolicy:
    if not isinstance(raw, dict):
        raise ConfigError("autoswitch.slotPolicies policy must be a JSON object")
    return SlotPolicy(
        five_hour_ceiling_pct=_finite_percentage(
            raw.get("fiveHourCeilingPct", DEFAULT_SLOT_POLICY.five_hour_ceiling_pct),
            "fiveHourCeilingPct",
        ),
        weekly_ceiling_pct=_finite_percentage(
            raw.get("weeklyCeilingPct", DEFAULT_SLOT_POLICY.weekly_ceiling_pct),
            "weeklyCeilingPct",
        ),
        priority=_policy_priority(raw.get("priority", DEFAULT_SLOT_POLICY.priority)),
    )


def _parse_slot_policy_table(
    raw_policies: object, backup_root: Path
) -> dict[int, SlotPolicy]:
    if not isinstance(raw_policies, dict):
        raise ConfigError("autoswitch.slotPolicies must be a JSON object")
    policies: dict[int, SlotPolicy] = {}
    for raw_slot, raw_policy in raw_policies.items():
        slot = _slot_number(raw_slot, backup_root)
        if slot in policies:
            raise ConfigError(
                f"autoswitch.slotPolicies has duplicate canonical slot {slot}"
            )
        policies[slot] = _parse_slot_policy(raw_policy)
    return policies


def _strict_autoswitch_section(backup_root: Path) -> dict:
    raw = _read_raw_for_write(settings_path(backup_root))
    section = raw.get("autoswitch", {})
    if not isinstance(section, dict):
        raise ConfigError("settings.json autoswitch must be a JSON object")
    return section


def load_shared_profile_settings(backup_root: Path) -> SharedProfileSettings:
    """Strictly load the opt-in feature gate; absence deliberately means off."""
    section = _strict_autoswitch_section(backup_root)
    shared = section.get("sharedProfile", {})
    if not isinstance(shared, dict):
        raise ConfigError("autoswitch.sharedProfile must be a JSON object")
    enabled = shared.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("autoswitch.sharedProfile.enabled must be true or false")
    rollout_spec = SETTING_SPECS["autoswitch.sharedProfile.rolloutStage"]
    rollout_stage = shared.get("rolloutStage", rollout_spec.default)
    if rollout_stage not in rollout_spec.choices:
        raise ConfigError(
            "autoswitch.sharedProfile.rolloutStage must be one of: "
            f"{', '.join(rollout_spec.choices)}"
        )
    manual_hold = shared.get("manualHold", False)
    if not isinstance(manual_hold, bool):
        raise ConfigError(
            "autoswitch.sharedProfile.manualHold must be true or false"
        )
    dwell_spec = SETTING_SPECS["autoswitch.sharedProfile.dwellSeconds"]
    dwell = shared.get("dwellSeconds", dwell_spec.default)
    if (
        isinstance(dwell, bool)
        or not isinstance(dwell, int)
        or not dwell_spec.lo <= dwell <= dwell_spec.hi
    ):
        raise ConfigError(
            "autoswitch.sharedProfile.dwellSeconds must be an integer "
            f"between {dwell_spec.lo:g} and {dwell_spec.hi:g}"
        )
    delta_spec = SETTING_SPECS[
        "autoswitch.sharedProfile.materialUsageDeltaPct"
    ]
    delta = shared.get("materialUsageDeltaPct", delta_spec.default)
    if (
        isinstance(delta, bool)
        or not isinstance(delta, (int, float))
        or not math.isfinite(delta)
        or not delta_spec.lo <= delta <= delta_spec.hi
    ):
        raise ConfigError(
            "autoswitch.sharedProfile.materialUsageDeltaPct must be a finite "
            f"number between {delta_spec.lo:g} and {delta_spec.hi:g}"
        )
    return SharedProfileSettings(
        enabled=enabled,
        rollout_stage=rollout_stage,
        manual_hold=manual_hold,
        dwell_seconds=dwell,
        material_usage_delta_pct=float(delta),
    )


def load_slot_policies(backup_root: Path) -> dict[int, SlotPolicy]:
    """Strictly load explicit current-slot policies.

    Unlike classic scalar settings, safety constraints never clamp or degrade
    to defaults after a malformed hand edit. Shared-profile activation can
    therefore fail closed instead of losing a protected ceiling.
    """
    section = _strict_autoswitch_section(backup_root)
    raw_policies = section.get("slotPolicies", {})
    return _parse_slot_policy_table(raw_policies, backup_root)


def set_slot_policy(
    backup_root: Path,
    slot: int | str,
    *,
    five_hour_ceiling_pct: object = DEFAULT_SLOT_POLICY.five_hour_ceiling_pct,
    weekly_ceiling_pct: object = DEFAULT_SLOT_POLICY.weekly_ceiling_pct,
    priority: object = DEFAULT_SLOT_POLICY.priority,
) -> SlotPolicy:
    """Validate and atomically replace one slot's known policy fields."""
    slot_number = _slot_number(slot, backup_root)
    policy = SlotPolicy(
        _finite_percentage(five_hour_ceiling_pct, "fiveHourCeilingPct"),
        _finite_percentage(weekly_ceiling_pct, "weeklyCeilingPct"),
        _policy_priority(priority),
    )
    path = settings_path(backup_root)
    with FileLock(settings_lock_path(backup_root)):
        raw = _read_raw_for_write(path)
        section = raw.get("autoswitch")
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise ConfigError("settings.json autoswitch must be a JSON object")
        raw_policies = section.get("slotPolicies")
        if raw_policies is None:
            raw_policies = {}
        if not isinstance(raw_policies, dict):
            raise ConfigError("autoswitch.slotPolicies must be a JSON object")

        # Validate the complete existing table before changing one row. This
        # refuses to conceal a malformed protected policy elsewhere.
        _parse_slot_policy_table(raw_policies, backup_root)

        key = str(slot_number)
        existing = raw_policies.get(key)
        entry = dict(existing) if isinstance(existing, dict) else {}
        entry.update(
            {
                "fiveHourCeilingPct": policy.five_hour_ceiling_pct,
                "weeklyCeilingPct": policy.weekly_ceiling_pct,
                "priority": policy.priority,
            }
        )
        raw_policies[key] = entry
        section["slotPolicies"] = raw_policies
        raw["autoswitch"] = section
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        atomic_write_json(path, raw)
    return policy


def unset_slot_policy(backup_root: Path, slot: int | str) -> bool:
    """Atomically remove one explicit slot policy without touching siblings."""
    slot_number = _slot_number(slot, backup_root)
    path = settings_path(backup_root)
    with FileLock(settings_lock_path(backup_root)):
        raw = _read_raw_for_write(path)
        section = raw.get("autoswitch")
        if not isinstance(section, dict):
            return False
        raw_policies = section.get("slotPolicies")
        if not isinstance(raw_policies, dict):
            if raw_policies is None:
                return False
            raise ConfigError("autoswitch.slotPolicies must be a JSON object")
        _parse_slot_policy_table(raw_policies, backup_root)
        key = str(slot_number)
        if key not in raw_policies:
            return False
        del raw_policies[key]
        if not raw_policies:
            del section["slotPolicies"]
        raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
        atomic_write_json(path, raw)
        return True


def effective_settings(backup_root: Path) -> list[tuple[SettingSpec, object, bool]]:
    """(spec, effective value, explicitly set?) per key, in registry order.

    "Set" means the key is present in the raw file — an explicit value equal
    to the default still counts — so `cswap config`'s "(default)" marker
    reflects the file, not value equality.
    """
    raw = _read_raw(settings_path(backup_root))
    loaded = {
        "autoswitch": load_settings(backup_root),
        "autoswitch.sharedProfile": load_shared_profile_settings(backup_root),
        "ui": load_ui_settings(backup_root),
    }
    rows = []
    for spec in SETTING_SPECS.values():
        section, _ = _nested_section(
            raw, spec.section, create=False, strict=False
        )
        is_set = isinstance(section, dict) and spec.json_key in section
        rows.append((spec, getattr(loaded[spec.section], spec.field), is_set))
    return rows


def merged_with_cli(settings: AutoSwitchSettings, args) -> AutoSwitchSettings:
    """Overlay non-None CLI overrides (argparse Namespace) onto settings."""
    overrides = {}
    for attr, field in (
        ("threshold", "threshold"),
        ("interval", "interval_seconds"),
        ("cooldown", "cooldown_seconds"),
        ("include_api_key_accounts", "include_api_key_accounts"),
        ("model", "model"),
        ("strategy", "strategy"),
        ("five_hour_threshold", "five_hour_threshold"),
        ("seven_day_threshold", "seven_day_threshold"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value
    if not overrides:
        return settings
    return _clamped(dataclasses.replace(settings, **overrides))


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON with the backup dir's 0600/0700 modes.

    Shared by settings.json and the autoswitch state file (and any future
    machine-local state files beside them).

    **Writes THROUGH a symlink, never over it.** A rename swaps a directory
    ENTRY and does not follow links, so renaming onto a symlinked path
    DETACHES the link: the write succeeds, the content is right, and the
    link target silently stops receiving updates — until something restores
    the link (a dotfiles deploy), taking every change written since with
    it. Same shape as #192/#193, which fixed ``session.py``'s own writer;
    this is the shared JSON writer. Three consequences, each deliberate:

    - A DANGLING link still writes where it points; linking a path is a
      request to write there.
    - The temp file is created beside the RESOLVED target, so the rename
      stays on one filesystem and remains atomic (beside the LINK it would
      hit EXDEV whenever the target lives on another mount).
    - The 0700 hardening stays on the directory cswap owns. Applying it to
      the resolved parent would narrow a directory belonging to something
      else, and raise ``PermissionError`` outright when that parent is not
      ours to chmod. The written file still gets 0600, and ``mkstemp``
      creates it 0600 to begin with, so the secret is never exposed.
    """
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        # `path.parent`, NOT the target's: see the docstring.
        os.chmod(path.parent, 0o700)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.close(fd)
        fd = -1
        replace_with_retry(tmp_path, str(target))
        if sys.platform != "win32":
            os.chmod(str(target), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
