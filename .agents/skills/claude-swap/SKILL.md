---
name: claude-swap
description: "Inspect and rotate the Claude Code sub-account store with the claude-swap CLI (`cswap`): read each managed account's 5-hour, weekly, and per-model usage windows with reset clocks, see which account is active, preview an autoswitch decision without acting, and rotate the global credential store when an account runs out of headroom. Use whenever a task touches Claude usage windows or account rotation: usage or rate-limit headroom needs checking, a 5h/weekly window is close to full, an agent hit a limit error, or the fleet needs moving to a different Claude sub-account."
user-invocable: false
---

# claude-swap

Multi-account switcher for Claude Code. Installed as both `claude-swap` and `cswap`.
It owns the **global** Claude credential store (`~/.claude/.credentials.json` plus `~/.claude.json`), so a switch is machine-wide, not per-terminal and not per-session.

Every command takes `--help`. `list`, `status`, and `switch` take `--json`; `auto` emits one JSON event per line.

## When to reach for it vs alternatives

Three tools sit near this, and picking the wrong one wastes a turn:

- **`claude-swap`** - reads and writes the Claude credential store. It is the only writer of that store (ADR 0031). Reach for it to see per-account 5h/weekly/per-model headroom with reset clocks, and to actually move the store to another account.
- **`quota-axi`** - read-only quota reporting across many providers (Claude, Codex, Cursor, Copilot, ...), plus the fleet's decision layer (`decide`) and its one mutation verb (`switch`), which actuates a Claude switch by shelling out to `claude-swap`. If the question is "how much runway across providers", start here. If it is "move the Claude store", that is claude-swap either directly or underneath quota-axi.
- **`bin/fm-switch-account.sh`** (firstmate, wrapped by the `jcode-switch-account` skill) - a completely different axis: it retypes jcode's per-session `/account claude switch <label>` into every live worker pane. It touches **jcode's** `auth.json` labels (`claude-otter`, `claude-fox`, ...), never claude-swap's numbered account slots. It does not move the Claude Code credential store, and claude-swap does not move jcode sessions.

Rule of thumb: harness is **claude** (the `claude` CLI, Claude Code, the VS Code extension) then claude-swap; harness is **jcode** then the fm-switch-account path. Slot numbers or emails means claude-swap; a `claude-*` label means the jcode path.

## Workflows

All of these are read-only and safe to run while agents are live.

```bash
cswap status                                     # active account + its 5h/7d/per-model windows and resets
cswap list                                       # every managed account with all its windows, active marked
cswap list --json | jq -r '.accounts[] |
  "\(.number) \(.email) 5h=\(.usage.fiveHour.pct) 7d=\(.usage.sevenDay.pct) active=\(.active)"'
cswap list --token-status                        # per-account token freshness + refresh-token presence
cswap auto --once --dry-run                      # what autoswitch would decide right now, human-readable
cswap auto --once --dry-run --model all --json   # same, machine-readable, counting per-model weekly windows
cswap slot-policy list                           # per-slot ceilings/priority and whether they are explicit
cswap slot-policy audit                          # slots occupied with no explicit policy
cswap config                                     # effective autoswitch settings and which are defaults
```

Deciding whether there is anywhere to go: `cswap auto --once --dry-run --json` prints one `poll` event carrying `headroomPct` per account, the active threshold, and every account's `windowsPct`, then a `no-switch` or switch event with a reason. That single call answers "is the active account near its limit" and "does a better account exist" together, so prefer it over eyeballing `list`.

Diagnosing a Claude auth failure that is not a usage limit: `cswap list --token-status` distinguishes a stale or refresh-token-less stored backup from a quota problem. A limit shows as a high window percentage in `status`; an auth problem shows in the token line.

Actually rotating (**mutating** - see the safety rules below before running any of these):

```bash
cswap switch --strategy best             # jump to the account with the most remaining headroom
cswap switch --strategy next-available   # rotate to the next account, skipping ones at their limit
cswap switch <num|email>                 # force a specific account
cswap auto                               # foreground polling loop that switches at the threshold
```

`switch` backs up the current login into the store before activating the target, and holds Claude Code's own advisory locks while it swaps (see `src/claude_swap/claude_locks.py`) so a concurrent token refresh cannot overwrite the swap. No restart is needed on Linux: Claude Code re-reads the credentials file, so running sessions adopt the new account on their next message.

## Fleet conventions

- **A switch here is global and it lands on every live Claude session on this box.** There is no per-session scoping, so during fleet hours a rotation moves everyone at once. It does not interrupt or kill in-flight work: the current turn keeps running and the next request goes out on the new account. What it does change is which account's window the rest of that work spends.
- **Never mutate the store to "test" something.** Every agent on this box, including you, is running against a live account. Verify with the read-only workflows above and with `--dry-run`; documenting a mutating command from source is correct, running one speculatively is not.
- Under ADR 0031 the routine fleet switch is orchestrated: `bin/fm-spawn.sh` consults `quota-axi decide` at spawn, and the watcher rotates on a live limit-error tripwire. A hand-run `cswap switch` bypasses that decision layer, so prefer letting the orchestrator act unless the captain asked for a specific account by hand.
- **The credential store keeps exactly one writer.** quota-axi never writes it natively; it shells out to claude-swap. Never edit `~/.claude/.credentials.json` or `~/.claude.json` by hand to move an account.
- Accounts are addressed by slot number or email; `swap`, `move`, and `alias` change addressing only, not credentials. This fleet's slots are `cyuan@hyfin.app`, `dev1@hyfin.app`, `dev2@hyfin.app`.
- Windows: the 5-hour session window and the weekly window are account-wide, and per-model weekly windows (`Fable`, `Opus`, ...) are separate. An account with plenty of account-wide headroom can still be exhausted for one model, and bare `auto` ignores per-model windows unless `--model` (or the `autoswitch.model` setting) names them. Passing `--model all` when comparing accounts is usually what this fleet wants.
- `slot-policy` defaults every occupied slot to a 90% 5h ceiling and 100% weekly with priority 0, and `audit` reports those as "no explicit policy". That is the normal state here, not a misconfiguration.
- `cswap run <num|email>` is the only non-global path: it launches Claude Code with `CLAUDE_CONFIG_DIR` pointed at a per-account session profile, leaving the default login and other terminals untouched. It is marked experimental and execs `claude`, so it is a terminal-session tool, not a fleet mechanism.

## Non-goals

- Not a jcode account switcher. jcode sessions carry their own account and are unreachable from this store; use `bin/fm-switch-account.sh` / the `jcode-switch-account` skill.
- Not a cross-provider quota reporter. It only knows Claude accounts it manages; for Codex, Cursor, Copilot, and friends use `quota-axi`.
- Not a decision layer for the fleet. Policy lives in quota-axi's registry and policy files; `slot-policy` and `autoswitch.*` settings only shape claude-swap's own autoswitch.
- Not a credential minter or a login flow. Adding an account still means logging in normally and then `cswap add`; recovery from a displaced credential is `/login` plus `cswap add`, never hand-editing the store.
- Not a flag reference. Run `--help` on any subcommand; this skill covers only what `--help` cannot know.
