# Anti-ping-pong dwell and material-usage trigger

## Decision

Shared-profile paced autoswitch has one controller-wide steady-state dwell,
not a per-slot cooldown:

```text
autoswitch.sharedProfile.dwellSeconds = 900       # default: 15 minutes
autoswitch.sharedProfile.materialUsageDeltaPct = 1.0  # default: 1 percentage point
```

`dwellSeconds` is a whole number in `[300, 3600]`.  Fifteen minutes is the
default; five minutes is the lowest supported value.  The lower bound is
longer than the normal decision-serving TTL and prevents one short polling
cycle from producing a switch storm.  The upper bound prevents an accidental
day-long hold from stranding an otherwise urgent slot.  The value applies to
the one shared managed profile, so a per-slot dwell would not protect the
actual switch boundary.

`materialUsageDeltaPct` is a finite number in `[0.5, 10.0]`, measured in raw
provider percentage points.  `1.0` is the default: large enough to reject
provider display/rounding noise, small enough that ordinary 24/7 Firstmate
work makes a paced re-evaluation possible promptly.  Zero, negative, NaN,
infinity, and values outside either range are strict shared-profile
configuration errors.  They do not silently fall back to an unsafe value.

These are new shared-profile-only settings.  Existing `autoswitch.cooldownSeconds`
continues to govern legacy `cswap auto`; enabling shared-profile mode does not
reinterpret a legacy value.  Store the new fields with shared-profile mode,
not inside an individual slot policy.  Show effective values and their source
in `cswap slot-policy audit` and the read-only TUI surface.

## Exact material-change predicate

When an activation commits, persist a `steady` record under the operational
state lock:

```text
steady(
  activeSlot,
  identity,
  policyRevision,
  activatedAt,
  dwellUntil = activatedAt + dwellSeconds,
  activationUsage = fresh successful locked-recheck observation
)
```

For each controlled window independently (`five_hour`, `seven_day`), let
`B(w)` be that baseline window and `F(w)` the active slot's successful,
current-selection-epoch provider observation.  A window contributes work
only when both observations contain finite `pct` values and belong to the
same provider reset generation: their nonempty `resets_at` values are equal,
or both are absent.  Its delta is:

```text
delta(w) = F(w).pct - B(w).pct
```

The active slot has **material fresh usage change** exactly when at least one
controlled window has:

```text
delta(w) >= materialUsageDeltaPct
```

Use raw fetched values; never rounded text, `countdown`, `clock`, fetch time,
remaining seconds, pacing score, priority, or slot number.  A reset-generation
change alone contributes no delta.  A percentage decrease, malformed window,
missing window, stale observation, failed fetch, policy change, or identity
change does not satisfy the predicate.  Therefore passage of time can alter
the paced score but cannot by itself release the active slot.

The baseline remains fixed for that activation.  Do not advance it after each
poll: consumption must accumulate until one material proof exists.  A later
switch replaces it only with the newly activated target's locked fresh
observation.

## Selection gate and exceptions

After confirmed priming has cleared, rank a fresh snapshot including the
active slot using *Paced selector semantics from configured ceilings*.  A
voluntary switch to a different top-ranked verified eligible slot requires
all of these:

1. `now >= dwellUntil`.
2. The active slot has material fresh usage change by the predicate above.
3. The target remains first in deterministic paced ordering when the active
   slot is included.
4. Under the operational lock, active identity, policy revision, `steady`
   record, both fresh observations, the predicate, ordering, and target
   eligibility still agree.  Commit target activation and new baseline in the
   same critical section.

`priming-pending` wins over this gate: its pin suppresses ranking and all
switching until its separate reset-advance proof succeeds.  Poll failures
while pinned only retry polling; they never use a dwell exception.

An active slot that is no longer verified eligible bypasses dwell and material
usage checks.  Immediately select the best *other* verified eligible target,
then perform the usual locked fresh recheck and record a new `steady` state.
This covers a ceiling hit, token quarantine, disabled slot, malformed policy,
or unreadable current-epoch usage.  If no other slot is verified eligible,
follow verification-blocked or reset-parking rules; do not invent a switch.

## Consequences and acceptance scenarios

- Equal snapshots may change pace order as reset clocks tick; no activation
  changes before real active usage reaches the configured delta and dwell ends.
- After 0.9 points of active consumption with default settings, no voluntary
  switch; after 1.0 point and 900 seconds, the current deterministic winner
  may switch.
- A 5-hour reset changing `resets_at` without a same-generation percentage
  increase does not unlock a switch.  It can only affect priming through its
  explicitly separate reset-advance proof.
- A selected target disappearing, becoming capped, or failing locked
  recheck aborts the selection; no stale fallback and no baseline overwrite.
- A capped or unreadable active slot may fail over immediately even one
  second after activation.  This is a safety failover, not pace churn.
- Two controller processes cannot each pass an old gate: lock-held recheck
  observes the first commit's new `steady` record and `dwellUntil`.

Required tests should cover each case above, strict configuration bounds,
restart persistence, and a score that crosses over solely because `now`
advanced.  The last case must remain on the active slot until both gates pass.

## Repository evidence

- `src/claude_swap/autoswitch.py` already persists `lastSwitchAt` behind
  `.autoswitch_state.lock` and repeats its cooldown check while holding that
  lock; shared-profile state should extend this one serialized decision
  boundary rather than add another loop.
- `src/claude_swap/usage_store.py` distinguishes current fetch state from
  cached decision values.  This decision requires a successful
  current-selection-epoch observation, stronger than serving TTL freshness.
- `src/claude_swap/oauth.py` retains provider `pct` and raw `resets_at` for
  `five_hour` and `seven_day`, while derived clocks are presentation data.
- *Define confirmed priming for the shared AFK queue* defines the distinct
  reset-advance proof and pin.  *Derive paced selector semantics from
  configured ceilings* supplies ranking and says cooldown plus observed work
  must prevent clock-only churn.
