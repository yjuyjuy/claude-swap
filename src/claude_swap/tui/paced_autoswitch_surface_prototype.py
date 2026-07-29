"""THROWAWAY PROTOTYPE — paced-autoswitch configuration and observability.

Question: How can a terminal-first operator configure slot policies without
accidentally changing a monthly roster, while seeing priming, pacing,
eligibility, fallback, and reset parking clearly?

This is deliberately a Textual-only prototype rather than a browser route:
claude-swap is a terminal/TUI application.  It has three materially different
layouts, switched with 1/2/3, and uses invented in-memory data only.  It never
reads settings.json, calls a provider, switches credentials, or writes state.

Run: uv run python -m claude_swap.tui.paced_autoswitch_surface_prototype

Delete after the Wayfinder decision is captured.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static


@dataclass(frozen=True)
class Slot:
    number: int
    occupant: str
    five_hour_pct: int
    weekly_pct: int
    five_hour_ceiling: int
    weekly_ceiling: int
    priority: int
    policy_source: str
    eligibility: str
    pace: str
    reset: str


SLOTS = (
    Slot(1, "max@team.test", 42, 31, 90, 100, 30, "explicit", "eligible", "0.036 pp/h", "5h 02:14 · 7d Fri 10:00"),
    Slot(2, "team@team.test", 61, 52, 90, 100, 20, "defaulted", "eligible", "0.029 pp/h", "5h 01:07 · 7d Sat 08:00"),
    Slot(3, "coworker@team.test", 48, 43, 50, 100, 10, "explicit", "eligible", "0.018 pp/h", "5h 03:21 · 7d Sun 13:00"),
    Slot(4, "vacant", 0, 0, 90, 100, 0, "vacant", "not selectable", "—", "—"),
)


@dataclass(frozen=True)
class ControllerCase:
    name: str
    summary: str
    active_slot: int
    state: str
    selection: str
    worker_admission: str
    next_action: str
    event: str


CASES = (
    ControllerCase(
        "confirmed priming",
        "Slot 1 pinned until provider confirms Firstmate opened/advanced 5h reset.",
        1,
        "priming-pending(slot=1, baseline=2026-07-29T11:00Z)",
        "ranking suppressed while pin exists",
        "permitted — independent Firstmate continues ordinary work",
        "poll only; no synthetic work, re-rank, or switch",
        "07:20 activated slot 1; waiting for post-activation 5h reset",
    ),
    ControllerCase(
        "steady paced selection",
        "Slot 1 is active; Slot 2 wins next eligible pacing score after dwell guard.",
        1,
        "steady(activeSlot=1, usageRevision=481)",
        "1) slot 2 pace 0.029  2) slot 1 pace 0.036 retained by dwell",
        "permitted",
        "poll; switch only after observed work change plus cooldown",
        "07:25 slot 1 remains active; clocks alone cannot trigger a switch",
    ),
    ControllerCase(
        "verification blocked",
        "Slot 1 is capped; slot 2 fetch is in Retry-After; slot 3 unreadable.",
        1,
        "verification-blocked(snapshotRevision=482)",
        "no verified eligible slot; not proven all-capped",
        "permitted — retain current profile; no parking switch",
        "bounded poll retry; expose per-slot unreadable reason",
        "07:30 slot 2 backoff, slot 3 missing weekly window",
    ),
    ControllerCase(
        "reset parking",
        "All slots freshly proved capped; worker hold confirmed before parking.",
        3,
        "reset-parking(parkedSlot=1, wakeAt=2026-07-29T14:17Z)",
        "all capped; earliest complete slot recovery is slot 2 at 14:17",
        "held — Firstmate confirmed work-permitted=false",
        "wake, fresh-poll every slot, select/recheck, then release hold",
        "07:35 worker hold observed; slot 1 is deterministic parking location",
    ),
)


CLI = """$ cswap slot-policy list
$ cswap slot-policy set 3 --5h-ceiling 50 --weekly-ceiling 100 --priority 10
$ cswap slot-policy unset 4
$ cswap slot-policy audit
$ cswap slot-policy audit --strict

Monthly roster: pause controller + drain/hold Firstmate; audit; atomically
apply intended slot policy table; roster move/swap; audit --strict; resume.
`move` and `swap` move identities. Policies stay on slot numbers."""


class PacedAutoswitchSurfacePrototype(App[None]):
    """Read-only layouts for a single terminal-native design question."""

    TITLE = "paced-autoswitch surface — prototype"
    CSS = """
    Screen { background: $background; color: $text; }
    #title { height: 3; padding: 1 2 0 2; color: $accent; text-style: bold; }
    #subtitle { height: 2; padding: 0 2; color: $text-muted; }
    #body { height: 1fr; padding: 0 2; }
    #left, #right, #single { width: 1fr; height: 1fr; padding: 1; border: round $panel; }
    #right { margin-left: 1; }
    #state { height: auto; max-height: 11; padding: 0 2 1 2; color: $text-muted; }
    Footer { background: $surface; }
    """
    BINDINGS = [
        Binding("1", "variant(0)", "1 split"),
        Binding("2", "variant(1)", "2 workflow"),
        Binding("3", "variant(2)", "3 ledger"),
        Binding("p", "cycle_case", "State"),
        Binding("r", "toggle_audit", "Audit"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.variant = 0
        self.case_index = 0
        self.audit_clean = True

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        yield Static(id="subtitle")
        with Horizontal(id="body"):
            yield Static(id="left")
            yield Static(id="right")
        yield Static(id="state")
        yield Footer()

    def on_mount(self) -> None:
        self._render()

    def action_variant(self, index: int) -> None:
        self.variant = index
        self._render()

    def action_cycle_case(self) -> None:
        self.case_index = (self.case_index + 1) % len(CASES)
        self._render()

    def action_toggle_audit(self) -> None:
        self.audit_clean = not self.audit_clean
        self._render()

    @property
    def case(self) -> ControllerCase:
        return CASES[self.case_index]

    def _render(self) -> None:
        names = ("1 — split operations", "2 — guarded workflow", "3 — seat ledger")
        self.query_one("#title", Static).update(
            f"THROWAWAY PROTOTYPE  ·  {names[self.variant]}"
        )
        self.query_one("#subtitle", Static).update(
            "Question: configure current slot policies safely; expose controller proof. "
            "Read-only invented data.  Keys 1/2/3 swap layouts; p changes state."
        )
        left = self.query_one("#left", Static)
        right = self.query_one("#right", Static)
        if self.variant == 0:
            left.update(self._policy_table())
            right.update(self._operations_rail())
        elif self.variant == 1:
            left.update(self._workflow())
            right.update(self._cli_drawer())
        else:
            left.update(self._ledger())
            right.update(self._proof_log())
        state = {
            "prototype": True,
            "layout": names[self.variant],
            "controller": asdict(self.case),
            "audit": "clean" if self.audit_clean else "blocked: protected slot policy missing",
            "slots": [asdict(slot) for slot in SLOTS],
        }
        self.query_one("#state", Static).update(
            "FULL IN-MEMORY STATE\n" + json.dumps(state, indent=2)
        )

    def _policy_table(self) -> Text:
        text = Text("SLOT POLICIES  (current slot keyed; identities never own policy)\n\n", style="bold")
        text.append("slot  occupant                  5h       7d       pri  source\n", style="dim")
        for slot in SLOTS:
            text.append(
                f" {slot.number:<3}  {slot.occupant:<24}  {slot.five_hour_pct:>2}/{slot.five_hour_ceiling:<3}  "
                f"{slot.weekly_pct:>2}/{slot.weekly_ceiling:<3}  {slot.priority:>3}  {slot.policy_source}\n"
            )
        text.append("\nEdit intent, not a live account. Explicit vs defaulted vs vacant stays visible.\n", style="dim")
        text.append("Protected current slot: 3  ·  hard 5h ceiling: 50%", style="bold yellow")
        return text

    def _operations_rail(self) -> Text:
        case = self.case
        text = Text("CONTROLLER OBSERVABILITY\n\n", style="bold")
        text.append(f"MODE     {case.name}\n", style="cyan")
        text.append(f"STATE    {case.state}\n")
        text.append(f"ACTIVE   slot {case.active_slot}\n")
        text.append(f"SELECT   {case.selection}\n")
        text.append(f"WORKER   {case.worker_admission}\n\n")
        text.append(f"NEXT     {case.next_action}\n", style="bold green")
        text.append(f"EVENT    {case.event}\n\n", style="dim")
        text.append("Why this state? 5h reset proof, current-epoch eligibility, worker-hold boundary, and planned wake are never hidden behind one generic 'auto' badge.", style="dim")
        return text

    def _workflow(self) -> Text:
        audit = "PASS — safe to resume" if self.audit_clean else "BLOCK — protected slot has no explicit policy"
        text = Text("MONTHLY ROSTER WORKFLOW\n\n", style="bold")
        steps = (
            ("1", "Pause controller; Firstmate drains or observes admission hold"),
            ("2", "Inspect effective policy table: explicit/defaulted/vacant"),
            ("3", "Atomically apply intended table; policies remain bound to slot numbers"),
            ("4", "Move/swap roster identities; policy does not follow identity"),
            ("5", f"Strict audit: {audit}"),
            ("6", "Refresh provider usage, then resume shared-profile controller"),
        )
        for number, label in steps:
            style = "bold green" if number == "5" and self.audit_clean else "bold red" if number == "5" else "bold cyan"
            text.append(f"[{number}] ", style=style)
            text.append(f"{label}\n")
        text.append("\nCurrent runtime proof\n", style="bold")
        text.append(f"{self.case.name}: {self.case.summary}\n")
        text.append(f"worker admission: {self.case.worker_admission}\n", style="yellow")
        return text

    def _cli_drawer(self) -> Text:
        text = Text("CLI IS AUTHORITATIVE FOR WRITES\n\n", style="bold")
        text.append(CLI)
        text.append("\n\nTUI role: explain effective policy and state; submit same dedicated operations, never nested JSON edits.", style="dim")
        return text

    def _ledger(self) -> Text:
        text = Text("SEAT LEDGER\n", style="bold")
        for slot in SLOTS:
            cap_state = "within cap" if slot.eligibility == "eligible" else slot.eligibility
            text.append(f"\nSLOT {slot.number}  {slot.occupant}\n", style="bold cyan")
            text.append(f"  policy: {slot.policy_source}; 5h {slot.five_hour_pct}% / {slot.five_hour_ceiling}% cap; 7d {slot.weekly_pct}% / {slot.weekly_ceiling}% cap\n")
            text.append(f"  selection: {cap_state}; pace {slot.pace}; priority {slot.priority}; resets {slot.reset}\n", style="dim")
        return text

    def _proof_log(self) -> Text:
        case = self.case
        text = Text("WHY NOTHING (OR SOMETHING) HAPPENS\n\n", style="bold")
        text.append(f"State: {case.state}\n\n", style="cyan")
        text.append(f"Event: {case.event}\n\n")
        text.append("Admission facts\n", style="bold")
        text.append("• strict below-cap checks use current provider fetch\n")
        text.append("• stale/backoff/missing data is ineligible, not fallback\n")
        text.append("• priming pin waits for real Firstmate work proof\n")
        text.append("• reset parking needs independently confirmed worker hold\n")
        text.append("\n", style="dim")
        text.append(f"Action: {case.next_action}", style="bold green")
        return text


if __name__ == "__main__":
    PacedAutoswitchSurfacePrototype().run()
