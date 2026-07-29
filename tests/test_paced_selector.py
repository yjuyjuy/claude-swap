"""Public-contract tests for shared-profile rotation pacing."""

from __future__ import annotations

from claude_swap.paced_selector import (
    material_usage_changed,
    rank_paced_slots,
)
from claude_swap.settings import SlotPolicy


NOW = 1_000_000.0


def _iso(seconds_from_now: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(NOW + seconds_from_now, timezone.utc).isoformat()


def _usage(
    five: float,
    weekly: float,
    *,
    five_reset: str | None = None,
    weekly_reset: str | None = None,
) -> dict:
    five_window = {"pct": five}
    weekly_window = {"pct": weekly}
    if five_reset is not None:
        five_window["resets_at"] = five_reset
    if weekly_reset is not None:
        weekly_window["resets_at"] = weekly_reset
    return {"five_hour": five_window, "seven_day": weekly_window}


def test_configured_maximum_weekly_headroom_drives_pacing():
    usage = {
        "1": _usage(10, 20, weekly_reset=_iso(100)),  # (80 - 20) / 100 = .6
        "2": _usage(10, 20, weekly_reset=_iso(120)),  # (100 - 20) / 120 = .667
    }
    policies = {
        "1": SlotPolicy(90, 80, 0),
        "2": SlotPolicy(90, 100, 0),
    }

    assert rank_paced_slots(usage, policies, NOW) == ["2", "1"]


def test_ties_use_open_5h_urgency_then_priority_then_numeric_slot():
    reset = _iso(100)
    usage = {
        "10": _usage(10, 50, five_reset=_iso(80), weekly_reset=reset),
        "2": _usage(10, 50, five_reset=_iso(80), weekly_reset=reset),
        "3": _usage(10, 50, five_reset=_iso(40), weekly_reset=reset),
        "4": _usage(10, 50, weekly_reset=reset),
    }
    policies = {
        "10": SlotPolicy(priority=5),
        "2": SlotPolicy(priority=5),
        "3": SlotPolicy(priority=-100),
        "4": SlotPolicy(priority=100),
    }

    assert rank_paced_slots(usage, policies, NOW) == ["3", "2", "10", "4"]


def test_unknown_weekly_clocks_are_eligible_fallbacks_only():
    usage = {
        "1": _usage(10, 10),
        "2": _usage(10, 90, weekly_reset=_iso(1000)),
    }
    policies = {"1": SlotPolicy(priority=100), "2": SlotPolicy()}

    assert rank_paced_slots(usage, policies, NOW) == ["2", "1"]


def test_equality_missing_and_malformed_windows_are_ineligible():
    usage = {
        "1": _usage(90, 10, weekly_reset=_iso(100)),
        "2": {"five_hour": {"pct": 10}},
        "3": _usage(10, float("nan"), weekly_reset=_iso(100)),
        "4": _usage(89.9, 99.9, weekly_reset=_iso(100)),
    }
    policies = {slot: SlotPolicy() for slot in usage}

    assert rank_paced_slots(usage, policies, NOW) == ["4"]


def test_material_usage_requires_same_reset_generation():
    baseline = _usage(
        10,
        20,
        five_reset="2026-08-01T00:00:00Z",
        weekly_reset="2026-08-02T00:00:00Z",
    )
    assert not material_usage_changed(
        baseline,
        _usage(
            10,
            20,
            five_reset="2026-08-01T01:00:00Z",
            weekly_reset="2026-08-02T01:00:00Z",
        ),
        1.0,
    )
    assert not material_usage_changed(
        baseline,
        _usage(
            10.9,
            20,
            five_reset="2026-08-01T00:00:00Z",
            weekly_reset="2026-08-02T00:00:00Z",
        ),
        1.0,
    )
    assert material_usage_changed(
        baseline,
        _usage(
            11,
            20,
            five_reset="2026-08-01T00:00:00Z",
            weekly_reset="2026-08-02T00:00:00Z",
        ),
        1.0,
    )
