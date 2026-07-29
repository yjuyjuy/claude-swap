# Verification and rollout safeguards for shared-profile autoswitch

## Release decision

Do not enable shared-profile mode for Firstmate until all gates in this note
pass.  The safety property is stronger than "a switch works": no provider
work may start on a rotating seat unless its current selection epoch and its
locked activation recheck prove both controlled windows are strictly below
their slot ceilings.  A failed proof is unavailable capacity, never a reason
to reuse cached usage, send warm-up work, or guess another target.

Firstmate remains the owner of its 24/7 queue.  Autoswitch activates
credentials, polls usage, and publishes admission state only; it must not
lease, dispatch, acknowledge, replay, or synthesize Firstmate work.  A test
double is insufficient for release: the real worker integration must provide
the same durable admission protocol before reset parking is enabled.

## Test matrix

Use a fake clock, deterministic provider responses, real temporary state
files, and a fake Firstmate admission endpoint.  Unit tests belong beside
`tests/test_autoswitch.py`; usage-store failure/claim cases belong in
`tests/test_usage_store.py`.  Add a process-level integration suite running
one controller and one worker against a temporary shared profile.

| Area | Required assertions |
| --- | --- |
| Policy load | Reject malformed/noncanonical slot keys, booleans, out-of-range ceilings and invalid dwell/delta.  Invalid policy makes shared mode inactive; no profile switch, admission release, or fallback.  Omitted fields resolve to 90/100/0 only in explicit shared mode. |
| Strict eligibility | Equality at either ceiling, stale cached result, 429-trusted result, missing/malformed window, backoff, active fetch claim, dead token, disabled slot, and identity mismatch are all ineligible.  A successful fetch in current epoch plus locked second fetch is required; a prior winner is never a fallback. |
| Pacing | Cross-multiply raw headroom/time values; test larger rate, 5-hour urgency tie, priority tie, canonical slot tie, and unscored availability fallback.  A lower weekly ceiling changes headroom immediately.  Past/missing reset cannot receive pace score. |
| Two-phase activation | Between snapshot and lock, mutate target pct, policy revision, slot identity, enablement, credentials, freshen outcome, and winner order one at a time.  Each mutation aborts activation, preserves active profile/state baseline, and starts a later epoch.  Concurrent controllers produce one committed switch only. |
| Confirmed priming | Persist pre-switch slot/identity/reset baseline and `activatedAt`; restart retains pin.  Only a later successful fetch (`fetchedAt > activatedAt`) with changed/advanced 5-hour reset clears it.  Pct rise alone, equal reset, missing reset, failed poll, 429, backoff, or claim does not.  While pinned, no rank/switch or synthetic provider request happens. |
| Steady dwell | Before 900 seconds or before 1.0 same-reset raw percentage-point increase, changed clocks/ranking do not switch.  Exactly 1.0 after dwell may switch.  Reset-generation change, decrease, missing/malformed value, identity/policy change, and stale result do not count.  Persist/restart baseline and make concurrent locked recheck reject a second switch. |
| Emergency failover | Active capped, invalid, disabled, quarantined, or unreadable bypasses dwell only to a different verified eligible slot.  If none exists, retain profile and enter verification-blocked/all-capped path; never switch to an unverified fallback. |
| Reset parking | Prove every enabled policy-valid non-quarantined seat capped in same epoch.  Per-seat recovery is later blocking-window reset; roster wake is earliest per-seat recovery plus slack.  Missing/past reset yields bounded retry, not long sleep.  Wake after suspend/clock jump discards snapshot, polls all seats, reranks, locked-rechecks, then releases admission. |
| Worker admission | Worker rejects beginning provider work when durable `work-permitted=false` is observed for controller revision.  Controller waits for observed hold before parking; no acknowledgement means no parking switch.  It releases only after fresh post-wake activation.  Existing in-flight work is allowed to finish once, but no request is replayed, duplicated, or acknowledged by autoswitch. |
| Compatibility | Classic `cswap auto`, API-key behavior, isolated `cswap run`, settings unknown-field preservation, and current JSONL consumers remain unchanged when shared mode disabled.  Dry run emits hypothetical state/reason only and performs no credential/state/admission write. |

### Fault injection

For every provider and worker fault below, assert three things: no ceiling
crossing activation, durable state carries exact reason, and Firstmate gets no
duplicate/new synthetic work.

- Provider: timeout, DNS/TLS error, HTTP 429 with `Retry-After: 0` and long
  Retry-After, 5xx, invalid grant, token identity conflict, partial JSON,
  non-finite pct, missing window, bad/past reset, reset moving backward, and
  a response that changes between selection and lock.
- Store/controller: reserved fetch held by another collector, state/settings
  lock contention, atomic-write interruption, corrupt/old state, restart at
  every transition, two controllers racing, slot move/swap/remove while a
  decision is pending, and wall-clock forward/backward jump or suspend.
- Firstmate: unreachable admission service, delayed/duplicate/stale
  acknowledgement, worker restart after hold, controller restart after hold,
  worker reports an old controller revision, an in-flight request at hold,
  and worker crash before/after observing release.  A stale acknowledgement
  must never release a newer hold.

Property/model tests should generate slot policies, finite usage values,
reset orders, response failures, and interleavings.  Invariants: committed
target is strictly below both ceilings at lock time; a pin never changes slot;
no voluntary switch lacks dwell plus material same-generation work; all-capped
requires complete readable proof; an admission release has a matching fresh
eligible activation; and every worker request maps to exactly one worker-owned
queue record.

## Observable proof

Extend existing additive JSONL events; do not expose tokens, raw credentials,
or Firstmate prompt/task content.  Every decision event needs:

- controller revision/selection epoch, state (`priming-pending`, `steady`,
  `verification-blocked`, `reset-parking`), active canonical slot, identity
  fingerprint, policy revision, and trigger;
- per-slot effective ceilings, observation revision/fetch age, eligibility
  result and precise exclusion reason, paced ordering keys, selected/fallback
  rationale, dwell remaining, and material-delta result;
- priming baseline reset, post-switch observation revision, proof result,
  consecutive failed polls, and escalation state;
- worker-admission desired/observed values, controller revision, hold/release
  timestamps, parked slot, wake time, and reset-proof inputs; and
- a terminal activation decision containing both pre-lock candidate revision
  and locked recheck revision.

Assertions for integration and canary telemetry:

1. Every `switch-committed` has a same-revision `eligibility-verified` event
   for target, strict values below both ceilings, then worker-visible active
   revision before Firstmate begins later work.
2. Every `work-permitted=true` has a preceding fresh eligible activation;
   every parked switch has preceding matching observed worker hold.
3. No unknown/stale/backoff/claimed observation leads to `switch-committed`.
   No priming failure changes active slot.  No same-reset below-delta/dwell
   decision changes active slot.
4. Event stream and persisted controller state reconcile by revision across
   restart.  Missing event/state pair is a release blocker, not telemetry
   noise.

`PollEvent`, `NoSwitchEvent`, and `SwitchEvent` are already additive JSONL
surfaces, while `autoswitch_state.json` is already lock-protected.  Preserve
that compatibility shape; add new event kinds/fields rather than changing
existing meanings.  Operator UI must show the same facts, especially
verification-blocked versus proven all-capped.

## Staged rollout

| Gate | Scope | Entry proof | Advance only when | Stop/rollback |
| --- | --- | --- | --- | --- |
| 0. Contract | No live actuation | Firstmate durable admission API tested against real worker build; no cswap queue ownership | Full matrix and property tests pass; schema/restart compatibility reviewed | Missing revisioned hold/release protocol: do not ship parking or shared mode. |
| 1. Shadow | Production observations, `--dry-run --json`; no switches/holds | Compare hypothetical choices/events with operator audit | Zero unexplainable decision, stale activation attempt, or schema/state reconciliation gap across normal reset cycles | Disable shadow; retain legacy mode; investigate from immutable event/state bundle. |
| 2. Canary | One owner-controlled unprotected slot; real Firstmate work | Fresh ceiling below safety cap, worker revision handshake, manual observer present | Confirmed priming, dwell, failure/restart, and one reset recovery complete with zero invariant failures | Publish hold, stop controller actuation; do not replay queue. |
| 3. Small roster | Owner-controlled slots only | Canary evidence plus clean rollback drill | Several 5-hour and weekly cycles, at least one backoff and controller/worker restart safely recovered | Same as canary; no automatic switch to a presumed-safe slot. |
| 4. Protected seat | Add 50% protected seat last | Audit shows intended current-slot policy; alerting and operator runbook exercised | Sustained observation confirms no protected-seat activation at/equal/above 50%, no duplicate work, no unexplained holds | Immediately hold worker admission; disable shared actuation pending manual fresh reconciliation. |

Gate thresholds are safety gates, not availability targets: **zero** ceiling
or admission invariant failures; **zero** duplicate/replayed Firstmate work;
**zero** unknown-usage activations; and all event/state records reconcilable.
Any loss of observability freezes at current gate.  Do not promote merely
because throughput improved.

## Rollback and incident conditions

Trigger immediate fail-closed hold and page operator for: observed usage at or
above a configured ceiling on active work; committed activation without
current locked proof; worker starts work while held; release without fresh
post-wake proof; duplicate/replayed queue record; policy/slot identity drift;
or unreadable/corrupt controller state during a pending hold/pin.

Rollback order matters:

1. Publish `work-permitted=false` with a new controller revision and wait for
   Firstmate observation.  Stop admitting new provider work; allow the
   worker's single in-flight request to report its own outcome.
2. Disable shared-profile actuation.  Do **not** bounce credentials or pick a
   fallback from stale data; that could consume another ceiling or disrupt a
   running worker.
3. Preserve state, policy snapshot, event JSONL, worker admission record, and
   usage observations for reconciliation.  Never clear a pin/hold to make a
   dashboard look healthy.
4. Operator performs a fresh all-seat poll, checks slot identity and effective
   policies, chooses a proven below-ceiling slot, and only then explicitly
   releases Firstmate.  Resume begins at shadow/canary gate appropriate to
   incident severity, never automatically at full rollout.

If worker-hold delivery itself is unavailable, retain current profile and
block shared-mode selection; do not park or attempt a credential rollback.
This may temporarily pause new Firstmate work, but preserves the protected
seat ceiling and avoids replay/duplication.  Legacy autoswitch remains an
explicit separate mode, not an automatic escape hatch.

## Repository evidence

- `AutoSwitchEngine._mutate_state` and `_perform` serialize persistent state
  under `.autoswitch_state.lock` (`src/claude_swap/autoswitch.py:518-540`,
  `1350-1390`); existing tests cover state preservation and lock-held
  double-switch prevention (`tests/test_autoswitch.py:1174-1186`, `2051-2096`).
- `UsageEntry` exposes fetch time, error/backoff, claim, and last-good state;
  `UsageStore.reserve` prevents duplicate in-flight fetches
  (`src/claude_swap/usage_store.py:130-180`, `557-617`).  Its legacy
  stale-on-error rules are deliberately not hard-ceiling proof.
- `EngineHarness` has fake clock, real temporary state, captured events, and
  provider patch seams (`tests/test_autoswitch.py:35-145`).  Existing tests
  cover Retry-After refusal then recovery (`920-968`), no long sleep for
  unknown recovery (`1867-1888`), and JSON event envelope compatibility
  (`1263-1271`).
- Existing events/CLI JSONL are additive surfaces
  (`src/claude_swap/autoswitch.py:150-249`, `src/claude_swap/cli.py:641-670`).
  Existing dry run makes no switch/state change (`tests/test_autoswitch.py:1189-1248`).
- This plan applies decisions in [confirmed priming](https://github.com/yjuyjuy/claude-swap/issues/3), [paced selector semantics](https://github.com/yjuyjuy/claude-swap/issues/4), [freshness and reset parking](https://github.com/yjuyjuy/claude-swap/issues/5), and [anti-ping-pong dwell](https://github.com/yjuyjuy/claude-swap/issues/7).
