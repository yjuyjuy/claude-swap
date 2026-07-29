# Slot-policy persistence and roster lifecycle

## Decision

Persist desired policies in `settings.json`, under
`autoswitch.slotPolicies`, keyed by canonical current slot number. A policy
belongs to a slot, not to an email, UUID, alias, credential, usage-cache row,
or session profile. Therefore an account moved to a new slot receives that
slot's policy; a policy never follows an account.

This matches the domain term **slot policy** and is the only interpretation
that makes a protected seat a deliberately protected current position rather
than an accidentally permanent property of one identity.

```json
{
  "schemaVersion": 1,
  "autoswitch": {
    "slotPolicies": {
      "1": { "fiveHourCeilingPct": 90, "weeklyCeilingPct": 100, "priority": 30 },
      "2": { "fiveHourCeilingPct": 90, "weeklyCeilingPct": 100, "priority": 20 },
      "3": { "fiveHourCeilingPct": 50, "weeklyCeilingPct": 100, "priority": 10 }
    }
  }
}
```

`priority` is optional and defaults to `0`; larger value wins only the final
tie-break and the one-time priming order. It must not override eligibility,
configured-maximum pacing, or 5-hour reset urgency.

For shared-profile mode, missing slots resolve to a mode default of 90% for
the 5-hour safety ceiling, 100% for the weekly ceiling, and priority 0. An
explicit policy replaces the whole defaulted policy, field by field. Legacy
`cswap auto` remains on its existing global settings unless shared-profile
mode is explicitly enabled; no existing configuration silently changes its
selection semantics.

## Validation and loading contract

- Keys are canonical positive decimal slot numbers (`"1"` through `"99"`;
  use the existing higher-number exception only if the roster already contains
  one). Reject `"01"`, zero, negatives, non-numeric keys, and duplicate keys
  after canonicalization.
- A policy is an object. `fiveHourCeilingPct` and `weeklyCeilingPct` are finite
  numbers in `[1, 100]`; `priority`, when present, is an integer in
  `[-1000, 1000]`. Reject booleans and nulls.
- Omitted `weeklyCeilingPct` defaults to 100, omitted
  `fiveHourCeilingPct` defaults to 90, and omitted `priority` defaults to 0.
  Explicit `100` means no policy cap for that window, not an unavailable
  value.
- A configured slot may be vacant. It is valid preconfiguration for the next
  monthly roster and must be shown as `vacant`, not discarded. Conversely,
  every occupied shared-profile slot should be shown as `defaulted` when it
  lacks an explicit entry, so an operator can distinguish an intentional
  default from a missing roster edit.
- Invalid hand-edited `slotPolicies` must fail closed for shared-profile
  activation: emit a configuration error and make no automatic switch. Do not
  silently drop a malformed protected-slot ceiling and then rotate onto it.
  The existing forgiving loader is suitable for old scalar preferences, but
  not for a safety constraint.
- Preserve unknown sibling settings and unknown per-policy fields on a
  read-modify-write. A newer CLI must not erase a newer controller's data.

The command surface should use dedicated operations rather than forcing a
nested JSON object through `cswap config set`: `cswap slot-policy set SLOT
--5h-ceiling PCT [--weekly-ceiling PCT] [--priority N]`, `unset SLOT`, `list`,
and `audit`. The exact final CLI/TUI presentation remains map fog, but every
writer must share the same parser and validator. Writes must be atomic and
serialized with a settings-file lock so concurrent UI/CLI edits cannot lose a
roster update.

## Migration and state boundaries

No data migration is required for existing installations. `slotPolicies` is
additive inside an already forward-compatible `settings.json`; absence means
the shared-profile feature is not enabled and classic autoswitch behavior is
unchanged. When an operator enables shared-profile mode, defaults are derived
at read time rather than backfilled into every slot. This avoids freezing a
policy onto account identities or writing stale policy records for slots that
do not exist yet.

Do not put policy in `autoswitch_state.json`. That file is operational state
(cooldown and quarantine) and is intentionally slot-keyed only long enough to
self-heal after credentials or a roster change. Policy is durable desired
configuration and survives restarts, replacement logins, and a new month.

Existing roster operations already relocate credentials, config backups,
session profiles, `sequence.json` records, and the active slot with the
account. Usage-cache identity checks and quarantine self-heal rather than
moving those records. `slotPolicies` must deliberately be the opposite:
leave it untouched on `move` and `swap`. That is what makes policy follow the
number.

## Monthly-roster workflow

1. Pause the shared-profile controller and drain/park its queue; do not edit
   roster or policy while automatic activation can run.
2. Inspect `cswap slot-policy audit`: current occupant, effective ceilings,
   priority, and `explicit/defaulted/vacant` status for every policy key.
3. Apply the complete intended slot-policy table atomically, including the
   protected slot's 50% 5-hour ceiling, before resuming work.
4. Use normal `add --slot`, `move`, `swap`, `remove`, `disable`, and `enable`
   commands to make `sequence.json` match the monthly roster. Moving an
   occupant onto the protected number intentionally gives it the protected
   ceiling; removing it leaves that slot's policy reserved for its replacement.
5. Run `audit --strict` after the roster edit. It must reject resume when a
   required occupied slot has no intended policy, a protected slot is above
   its declared hard ceiling, or an unexpected occupied slot remains. Then
   resume controller and refresh usage before first activation.

This order makes the protected ceiling explicit and observable, while the
pause prevents a transient old/new roster combination from selecting an
account under the wrong policy.

## Repository evidence

- `src/claude_swap/settings.py` stores `autoswitch` settings atomically,
  preserves unknown top-level keys, and already distinguishes forgiving reads
  from strict write validation.
- `src/claude_swap/autoswitch.py` keeps cooldown and quarantine in
  `autoswitch_state.json` under a dedicated lock; it is not configuration.
- `src/claude_swap/switcher.py` documents that `swap_accounts` and
  `move_account` relocate the account-owned material with a slot change,
  while usage and quarantine records self-heal by identity. This is the
  relevant boundary for leaving slot policy in place.
- `CONTEXT.md` defines slot policy, protected seat, per-seat ceiling,
  configured-maximum pacing, and priority's limited role.
