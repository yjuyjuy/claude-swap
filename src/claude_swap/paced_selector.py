"""Configured-maximum pacing for shared-profile rotation."""

from __future__ import annotations

import math

from claude_swap.poll_policy import parse_reset_ts
from claude_swap.settings import SlotPolicy


def _finite_pct(window: object) -> float | None:
    if not isinstance(window, dict):
        return None
    value = window.get("pct")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    return float(value)


def verified_eligible(usage: object, policy: SlotPolicy) -> bool:
    """Whether one fresh provider observation is strictly below both ceilings."""
    if not isinstance(usage, dict):
        return False
    five = _finite_pct(usage.get("five_hour"))
    weekly = _finite_pct(usage.get("seven_day"))
    return (
        five is not None
        and weekly is not None
        and five < policy.five_hour_ceiling_pct
        and weekly < policy.weekly_ceiling_pct
    )


def _slot_number(slot: str) -> int:
    try:
        return int(slot)
    except (TypeError, ValueError):
        return 2**63 - 1


def rank_paced_slots(
    usage_by_slot: dict[str, dict | str | None],
    policies: dict[str, SlotPolicy],
    now: float,
) -> list[str]:
    """Rank verified eligible slots by pace, 5h urgency, priority, and slot."""
    scored: list[tuple[tuple, str]] = []
    fallback: list[tuple[tuple, str]] = []
    for slot, usage in usage_by_slot.items():
        policy = policies.get(slot)
        if policy is None or not verified_eligible(usage, policy):
            continue
        assert isinstance(usage, dict)
        weekly = float(usage["seven_day"]["pct"])
        weekly_reset = parse_reset_ts(usage["seven_day"].get("resets_at"))
        five_reset = parse_reset_ts(usage["five_hour"].get("resets_at"))
        open_reset = (
            five_reset
            if five_reset is not None and five_reset > now
            else float("inf")
        )
        tie = (open_reset, -policy.priority, _slot_number(slot))
        if weekly_reset is not None and weekly_reset > now:
            rate = (policy.weekly_ceiling_pct - weekly) / (weekly_reset - now)
            scored.append(((-rate, *tie), slot))
        else:
            fallback.append((tie, slot))
    scored.sort(key=lambda item: item[0])
    fallback.sort(key=lambda item: item[0])
    return [slot for _, slot in (*scored, *fallback)]


def material_usage_changed(
    baseline: object,
    fresh: object,
    delta_pct: float,
) -> bool:
    """Prove raw same-reset utilization grew by the configured delta."""
    if not isinstance(baseline, dict) or not isinstance(fresh, dict):
        return False
    for key in ("five_hour", "seven_day"):
        before = baseline.get(key)
        after = fresh.get(key)
        before_pct = _finite_pct(before)
        after_pct = _finite_pct(after)
        if before_pct is None or after_pct is None:
            continue
        assert isinstance(before, dict) and isinstance(after, dict)
        before_reset = before.get("resets_at")
        after_reset = after.get("resets_at")
        same_generation = (
            before_reset == after_reset
            if before_reset or after_reset
            else True
        )
        if same_generation and after_pct - before_pct >= delta_pct:
            return True
    return False
