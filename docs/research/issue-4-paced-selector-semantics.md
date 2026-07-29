# Paced selector semantics from configured ceilings

## Decision

Shared-profile autoswitch selects among **verified eligible** slots.  A slot
is verified eligible only when it is enabled, has a valid effective slot
policy, and its fresh provider snapshot places both controlled windows
strictly below their own ceilings:

```text
fiveHour.pct < fiveHourCeilingPct
sevenDay.pct < weeklyCeilingPct
```

Equality is ineligible.  A ceiling is a safety boundary, not a target that
may be reached and then crossed during an unattended poll interval.  Missing,
unreadable, stale, or otherwise unverified usage is ineligible; reset-time
freshness and reset-parking details are owned by *Specify freshness and
reset-parking safety guarantees*.

This is distinct from legacy `best` and `consume-first`.  The new
shared-profile selector is enabled only by the explicit mode from *Specify
slot-policy persistence and roster lifecycle*; legacy modes retain their
current threshold semantics.

## Weekly pacing score

For a verified eligible slot `s`, define:

```text
C(s) = effective weeklyCeilingPct
U(s) = fresh sevenDay.pct
H(s) = C(s) - U(s)                    # configured weekly headroom
T(s) = sevenDay.resetsAt - now        # seconds remaining, strictly positive
R(s) = H(s) / T(s)                    # required configured-weekly spend rate
```

`R` is measured in percentage points per second.  It answers: *to consume
this slot's allowed weekly capacity by its own reset, how quickly must it be
used from now?*  The primary paced choice is the eligible slot with the
largest `R`; it is furthest behind the rate needed to reach its configured
maximum.  Comparisons use raw provider percentages and timestamps (or
cross-multiplication of `H` and `T`), never rounded display values.

Using `C`, not provider `100`, is essential.  A slot capped at 80% with 20%
used has 60 points of scheduled capacity, not 80; reducing the configured
ceiling immediately reduces the planned weekly spend without any second
policy knob.

A future, parseable weekly reset is required to receive a pacing score.  A
slot that otherwise passes the ceilings but lacks that score is still usable
as an **availability fallback**; it is not silently treated as resetting now
or as having infinite urgency.

## Deterministic ordering

Within the paced set, use this lexicographic ordering:

1. Larger `R` first.
2. An already-open, confirmed 5-hour window with the sooner future
   `fiveHour.resetsAt` first; a missing, closed, past, or unverified 5-hour
   reset sorts after every confirmed open window.
3. Larger configured slot `priority` first.
4. Lower canonical slot number first.

The second key is **5-hour reset urgency**.  It can decide between equally
urgent weekly allocations, but never overrides a weekly pacing difference or
either ceiling.  Priority is intentionally only the final policy tie-break;
it is not a steady-state allocation rule.  The numeric slot tie-break makes
the decision reproducible when policies and observations are identical.

If no verified eligible slot has a weekly pacing score, select the verified
eligible fallback using keys 2--4.  This is availability fallback: the
controller must not deliberately idle a usable shared profile merely because
a weekly clock was omitted.  If there is no verified eligible slot, enter the
reset-parking/recovery path rather than relaxing a ceiling.

## State machine and switching guard

The selector state is persisted alongside the operational autoswitch state;
it is keyed by slot and stores no durable policy.  It has these externally
meaningful states:

```text
disabled
  -> priming-pending(slot, baseline, activatedAt)
  -> steady(activeSlot, activatedAt, activeUsageRevision)
  -> selecting(reason, snapshotRevision)
  -> steady(...)
  -> reset-parking | recovery
```

`priming-pending` is the pin specified by *Define confirmed priming for the
shared AFK queue*.  While it exists, normal ranking is suppressed.  Only a
successful provider poll whose 5-hour reset proves real Firstmate work has
opened/advanced the window can advance that slot to `steady`; failed polls
retain the pin and retry polling only.

In `steady`, each decision runs from one fresh, immutable selection snapshot.
Before activating the nominated target, acquire the existing operational
lock, reload state, refresh/recheck that target's eligibility, and commit the
same target only if the snapshot is still valid.  A changed winner, a new
cap, a failed freshening, or a failed verification abandons the decision and
causes a later fresh selection; it never activates a stale fallback.

To prevent switching merely because clocks tick, preserve the eligible active
slot until both of these are true:

- the existing autoswitch cooldown/minimum residence period has elapsed; and
- fresh usage shows work-related change since that slot was activated (or the
  active slot has become ineligible).

Once those guards pass, a different top-ranked verified candidate may replace
the active slot.  Ineligibility of the active slot bypasses the dwell guard:
the selector immediately takes the best verified candidate, then records a
new `steady` activation.  The locked recheck must repeat the cooldown/dwell
test so concurrent controller processes cannot make two successive switches.

This produces deliberate rotation as Firstmate consumes capacity, while an
idle profile cannot bounce between slots merely because their reset clocks
advance.  The active slot is not permanently preferred: its observed work
changes satisfy the next selection epoch, and a capped slot is removed from
the eligible set.  With scored candidates, a slot retaining weekly headroom
becomes increasingly urgent as its own reset approaches; it therefore cannot
be starved by a lower-urgency peer unless its 5-hour safety ceiling or a
provider-freshness gate correctly makes it unavailable.

## Required decision outcomes

| Condition | Outcome |
| --- | --- |
| Priming proof outstanding | Keep the priming pin; poll only; do not rank or send work. |
| Active and candidates verified eligible | Apply paced ordering, subject to steady-state dwell guard. |
| Active becomes ineligible | Ignore dwell guard; activate first freshly rechecked ranked candidate. |
| No scored candidates, fallback exists | Activate fallback by 5-hour urgency, priority, slot number. |
| No verified eligible candidate | Reset park / automatic recovery; never exceed a ceiling. |
| Candidate changes or loses freshness before activation | Abort; refresh and rerank on a new snapshot. |

Autoswitch remains a credential activator and usage poller only.  It neither
leases, dispatches, acknowledges, nor replays Firstmate work, and it sends no
synthetic prompt to make the scores move.

## Repository evidence

- `src/claude_swap/autoswitch.py` already separates candidate ranking from
  freshen-and-switch, persists cooldown under a lock, and rechecks cooldown
  under that lock.  The shared selector should preserve those race and
  thrash protections rather than add a second unsynchronized loop.
- Its current `consume-first` key chooses earliest weekly reset and treats
  the 5-hour threshold as the landing gate.  That is not configured-maximum
  pacing: it ignores a slot's remaining capacity below a custom weekly cap.
- `src/claude_swap/pace.py` establishes that weekly reset cycles are seven
  days and derives time data from provider reset timestamps; the selector can
  use the same raw values without reusing its display-only ahead-of-pace
  marker.
- `CONTEXT.md` defines configured-maximum pacing, availability fallback,
  reset urgency, verified eligibility, confirmed priming, and priority's
  deliberately limited role.
