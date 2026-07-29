# Freshness and reset-parking safety guarantees

## Decision

Shared-profile autoswitch treats every per-seat ceiling as a hard admission
boundary. A rotating seat is eligible for ordinary activation only when one
successful provider fetch in the current selection epoch proves that every
controlled window is strictly below that seat's configured ceiling:

```text
fiveHour.pct < fiveHourCeilingPct
weekly.pct   < weeklyCeilingPct
```

Equality is capped. A cached measurement, an otherwise decision-trusted 429
measurement, a missing window, a malformed value, a fetch failure, a live
fetch claim, or a retry-backoff refusal is **unreadable**, hence ineligible.
This is deliberately stronger than the legacy engine's stale-on-error
behavior. A shared-profile ceiling cannot rely on a lower bound whose real
usage may have risen since it was observed.

An activation has two checks:

1. Build one selection snapshot from successful current-epoch provider
   observations. Rank only its verified eligible seats using *Paced selector
   semantics from configured ceilings*.
2. Under the autoswitch operational lock, reload slot policy, slot identity,
   and state; freshen credentials; fetch the nominated seat again; then
   revalidate the two strict comparisons before `switch_to`. Any changed
   identity/policy/state, failed fetch, or failed freshening aborts the
   selection and starts a later epoch. It must not use an old winner or slide
   to a stale fallback.

Persist the selection epoch/revision with operational state, not desired
policy. `UsageEntry.fresh()` alone is insufficient: its 180-second serving
TTL says nothing about whether a result was fetched for this selection or
after the lock recheck.

## Unreadable usage

Unreadable means unavailable, never optimistic fallback. It has three safe
outcomes:

- A different seat with a verified current snapshot may be selected.
- A priming pin remains pinned; only polling retries, as decided in *Define
  confirmed priming for the shared AFK queue*.
- With no verified eligible seat, enter `verification-blocked`, retain the
  current profile, honor usage-store backoff/claim ownership, and retry at
  the bounded next poll. Do not call the state `all-capped`, infer a reset,
  or sleep until some other seat's clock.

The mode must emit the per-seat reason (`fetch-failed`, `backoff`, `claimed`,
`missing-window`, `malformed-window`, or `stale`) so an operator can separate
capacity exhaustion from an observation failure. A permanently dead token is
quarantined under existing rules and is not an eligible or parkable seat until
the normal credential-replacement recovery.

## Reset parking

`all-capped` is a narrower, proven state: every enabled, policy-valid,
non-quarantined rotating seat has a successful current-epoch observation and
at least one controlled window at or above that seat's configured ceiling.
It is not enough that every seat is merely ineligible; an unreadable seat
keeps the state `verification-blocked`.

For a capped seat `s`, let `B(s)` be its controlled windows whose observed
usage is at/above their configured ceiling. Its first possible recovery is:

```text
releaseAt(s) = max(resetAt(w) for w in B(s))
```

Every `resetAt` in that expression must be parseable and strictly future.
The maximum is necessary: a seat at its 5-hour and weekly ceilings remains
ineligible after only the first reset. The recovery wake is:

```text
wakeAt = min(releaseAt(s) for every capped seat s) + RESET_SLACK_S
```

If any capped seat lacks a proved future recovery time, do not choose a later
known reset and oversleep it; use bounded retry polling instead. Past reset
timestamps are likewise not evidence of recovery: refresh now rather than
sleeping on an obsolete snapshot.

On proven all-capped state persist:

```text
reset-parking(parkedSlot, cappedSnapshotRevision, wakeAt, reason="all-capped")
```

`parkedSlot` is the enabled, policy-valid seat with highest configured
priority, then lowest canonical slot number. It is a deterministic credential
parking location, not an eligibility result and not permission to run work.
Desired slot policy remains in `settings.json`; this record is restart-safe
operational state only.

## Required worker-hold boundary

Profile-only parking is not safe with the confirmed deployment: Firstmate is
an independently running 24/7 worker. If autoswitch changes the shared
profile to a known capped `parkedSlot`, ordinary Firstmate requests could
immediately consume more capacity and violate the hard ceiling. Autoswitch
must still not lease, dispatch, acknowledge, replay, or synthesize work.

Therefore a park transition may switch to `parkedSlot` **only after** the
independent worker has durable, observable admission state `work-permitted =
false` and stops beginning provider work. That worker-owned hold is not a
cswap queue operation; cswap publishes capacity state and waits for the
worker's hold observation/confirmation. On wake, cswap first obtains a fresh
eligible snapshot and activates it, then publishes `work-permitted = true`.

Until that hold protocol exists, the fail-closed behavior is to retain the
currently active profile while in `verification-blocked` or `all-capped`;
do not perform a parking switch. This is the only way to claim a ceiling
guarantee without giving cswap authority over Firstmate's work queue.

## Wake and recovery contract

The loop sleeps only until `wakeAt`, in bounded `MAX_SLEEP_S` chunks and with
the existing wake interrupt. At or after the planned time (including resume
from laptop sleep), it must discard the capped selection snapshot, perform a
fresh provider poll for every enabled seat, and rerank the entire roster.
It must not assume a reset occurred, preselect the parked seat, or reactivate
work from a cached `resets_at` value.

If that wake poll is unreadable or still finds no eligible seat, retain the
hold and retry under normal fetch backoff. If a seat is verified eligible,
exit reset parking, activate it through the locked fresh recheck, record the
new steady state, and only then release worker admission. A newly selected
seat still enters the confirmed-priming pin when its 5-hour window lacks the
proof required by *Define confirmed priming for the shared AFK queue*.

## Required state outcomes

| Condition | State and action |
| --- | --- |
| Priming proof pending | Keep `priming-pending`; poll only. |
| Candidate snapshot or locked recheck unreadable | Ineligible; abandon selection; bounded retry. |
| Some seats capped, another unreadable, no eligible seat | `verification-blocked`; no reset-derived long sleep. |
| All seats freshly proved capped, all blocking resets known | Worker hold, then `reset-parking`; sleep until earliest slot recovery plus slack. |
| All seats freshly proved capped, any recovery unknown | Worker hold; bounded retry, no long reset sleep. |
| Wake occurs | Fresh-poll all seats, rerank, locked recheck winner, then release worker admission. |

## Acceptance tests

- A 181-second-old 429-trusted snapshot below a ceiling cannot activate a
  seat; a current successful recheck strictly below both ceilings can.
- Equality at either configured ceiling is ineligible, including a weekly
  ceiling below 100%.
- Fetch backoff, a concurrent claim, missing usage window, malformed usage,
  and a changed slot policy each abort the activation without stale fallback.
- A seat capped on 5-hour and weekly windows wakes only after the later of
  those two configured-threshold-blocking resets; the roster wakes at the
  earliest such seat recovery plus slack.
- Missing or past reset information yields bounded retry, never a sleep toward
  another seat's later timestamp.
- Wake after a wall-clock jump polls every seat and reranks; it does not reuse
  the parked snapshot.
- A park switch cannot occur until worker hold is observed, and worker
  admission cannot resume until fresh post-wake eligibility succeeds.

## Repository evidence

- `src/claude_swap/usage_store.py` allows a 180-second `fresh()` result, a
  300-second ordinary decision value, and deliberately extended stale-on-429
  trust. Its locked `reserve()` can also decline a fetch because another
  collector owns it or a backoff is active. Those useful legacy polling rules
  are not proof for a hard per-seat activation ceiling.
- `src/claude_swap/autoswitch.py` currently applies a per-target fresh gate
  only to `consume-first`; other target paths can select from decision-trusted
  values, and `_perform()` serializes switching without a usage recheck.
- `poll_policy.limiting_reset_ts()` and
  `AutoSwitchEngine._earliest_recovery()` already demonstrate the needed
  maximum-per-seat, minimum-across-roster reset shape, but compare only
  provider 100% exhaustion. Shared profile mode must compare configured
  ceilings instead.
- `tests/test_autoswitch.py` covers no long sleep for mixed unknown/exhausted
  inputs, later-reset recovery for a multi-window blocked account, and
  bounded fallback when reset information is absent. The acceptance tests
  above extend those guarantees to slot ceilings and worker admission.
- [Specify slot-policy persistence and roster lifecycle](https://github.com/yjuyjuy/claude-swap/issues/2)
  makes policy strict and fail-closed; [Define confirmed priming for the
  shared AFK queue](https://github.com/yjuyjuy/claude-swap/issues/3) assigns
  real work to independently running Firstmate and prohibits synthetic work;
  [Paced selector semantics from configured ceilings](https://github.com/yjuyjuy/claude-swap/issues/4)
  requires fresh verified eligibility and hands this recovery detail here.
