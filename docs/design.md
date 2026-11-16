---
title: Design
---

# slots — design

Status: draft for review (step 1 of `PROMPT.md` §4). No code is written until this is approved.

## 0. Changes from `PROMPT.md`

| Topic | `PROMPT.md` | This design |
|---|---|---|
| Devices | One laptop | **Single machine by default, offline.** An optional `[cluster]` mode, switched on in config, lets several machines share chosen resources (e.g. `e2e`, the employee locks) while each keeps its own CPU/RAM pools. |
| Per-device limits | One set of limits | One config file can hold `[device.<name>]` overrides, picked by hostname, so the same file works on the laptop and the desktop. |
| OS | macOS + Linux | Unchanged: macOS + Linux. Windows is out of scope. |
| Human interface | Dashboard is one part of several | **The dashboard is your single point of contact** (agents use the CLI). It gets a "Needs you" panel, an activity feed and live limit changes (§9). |
| Agent-slot expiry | Heartbeat expiry | **No inactivity timeout** by default. Session slots end only on release, session end or a dead pid, and idle ones are flagged to you (§1). |
| Setup | `pipx install` + several commands | **`uv tool install slots && slots setup`**. `setup` is one guided command that writes the config, installs the Claude Code hooks (showing the diff first), and offers the shims. The daemon starts on demand, so there is nothing to install as a service unless you want it at login. |

## 1. Broker choice: an on-demand local daemon

**Decision: a small asyncio daemon (`slots daemon`), one per machine, started automatically by the first CLI call that needs it.** Clients talk to it over a Unix socket. State is in SQLite (stdlib, WAL mode).

Why a daemon rather than file locks without one:

- **Event-driven FIFO.** The daemon owns the queue and wakes the next waiter the moment a lease ends. With `flock` alone, waiters either poll or race for the lock, and FIFO order needs a separately locked queue file (problem 4 today).
- **Leases that outlive a process.** An agent slot is held across a whole session, not one command. That needs pid checks and activity tracking, which are jobs for a process that is always running.
- **Cluster mode.** Sharing a lock across machines needs something to listen on the network. With a daemon in place, cluster mode is just a forwarding rule, not a second design.
- **Portability.** Only `AF_UNIX` sockets and SQLite are used, and both work the same on macOS and Linux. `lockf(1)` (problem 5) is gone.

"Super simple" is kept because the daemon is invisible. `slots <anything>` connects to the socket and, if nothing is listening, starts the daemon detached (`setsid`) and retries. A single-instance guard (`flock` on `daemon.pid`) stops two daemons from racing at startup. `slots daemon install` (a launchd agent or systemd user unit) is optional and only starts it at login.

### Lease liveness

Each lease records `owner = {lane, session_id?, pid, pid_start_time, host}` and has a kind:

| Kind | Used by | Ends when |
|---|---|---|
| `conn` | `slots run`, the shims, `tenantLock` | the client's socket closes (exit, crash, `kill -9`): instant, like `lockf` today |
| `pid` | `slots acquire --pid N` | that pid is gone. Checked on a 2 s sweep, and `pid_start_time` stops a reused pid from keeping a lease alive |
| `session` | agent slots held across a session | the Claude Code session ends (SessionEnd hook) or its pid dies. **There is no inactivity timeout**, because some sessions legitimately hold a slot for many hours. Hooks still record activity on every tool call, but only to show busy, idle or `idle?` in the dashboard, where you decide. A pool can opt in to expiry with `ttl = "2h"`, which is off by default. |

Every lease also ends on `release` by its owner, or on an admin action (dashboard or `--admin`), which is logged.

### Failure modes

| Failure | What happens |
|---|---|
| Client `kill -9` during `slots run` | The socket closes, so the lease ends and the next waiter is woken in under 1 ms. |
| Session crashes while holding an agent slot | The pid sweep frees it within 2 s. |
| Session alive but paused or forgotten | The lease is kept on purpose. The dashboard shows `idle?` after `warn.idle_min` with no lane processes and `long-held` after `warn.long_held_h`, and puts it in the **Needs you** panel. Releasing it is your call, with one key. |
| Session pid couldn't be found | The lease is marked `unverified` in the dashboard, since only SessionEnd, `release` or you can end it. |
| Daemon `kill -9` | Leases are in SQLite. The next CLI call starts a new daemon, which reloads them and runs a sweep straight away: dead pids are dropped, and session leases whose session is still alive are kept. `conn` leases are kept for a 10 s grace period while their clients reconnect with their lease token (each `slots run` client retries the socket). Unclaimed ones are dropped. Waiters reconnect and keep their original queue position, which they hold by token. |
| Daemon dies while nobody is running | Nothing happens. It restarts on the next call. |
| Corrupt or missing DB | The daemon renames it to `state.db.broken-<ts>`, starts empty, and logs this. Every live client re-claims on reconnect. |
| Clock jump | Heartbeat deadlines use the monotonic clock while the daemon is running, and wall time only across restarts. |

## 2. Cluster mode (optional)

```toml
[cluster]
enabled = false          # default: this machine only, nothing on the network
role    = "hub"          # "hub" on one machine, "member" on the rest
listen  = "0.0.0.0:7787" # hub only
hub     = "laptop.tail1234.ts.net:7787"   # members only
offline = "wait"         # shared resource while the hub is unreachable: wait (default) | fail

[lock.e2e]
scope = "shared"         # local (default) or shared
```

- **Every resource is `local` by default.** CPU and memory pools (`agent`, `test`) should stay local, since they protect one machine. Only resources that are truly shared, like the test employee or the mock core, are marked `shared`.
- **Each machine still runs its own daemon,** and local resources never touch the network, so a member works fully offline for everything local. The member daemon forwards `shared` requests to the hub over one TCP connection, and the hub runs the same broker code.
- **Liveness across machines.** The member's connection to the hub carries a heartbeat every 5 s. If a member goes silent for `grace` (default 60 s), the hub ends its shared leases and wakes the next waiter. When the member reconnects, it re-claims leases that are still live locally.
- **Security, kept to one step.** `slots cluster init` on the hub creates a self-signed TLS certificate and a random token, and prints a join line:
  `slots cluster join slots://laptop:7787#token=…&fp=sha256:…`
  Running that line on a member writes its config. The member pins the certificate by fingerprint, so no CA is needed. All traffic is TLS (stdlib `ssl`). The docs recommend Tailscale or a LAN, and never an open port on the internet.
- **The dashboard and `status --json`** show local state plus, in cluster mode, the hub's view of shared resources and the other devices' summaries (load, memory, slots in use) that they report with their heartbeat.

## 3. Config

One TOML file (`tomllib` is in the stdlib). It is found via `--config`, then `$SLOTS_CONFIG`, then `./.slots.toml` (searched upward), then `~/.config/slots/config.toml`.

```toml
[lanes]
root    = "~/Documents/dev/interface"
pattern = '^(?P<ticket>\d+)$'          # ticket id from the folder name

[pool.agent]
size = 7
hold = "session"                       # held until the session ends or dies; used by the hooks
# ttl = "2h"                           # optional auto-expiry when inactive; off by default
[pool.test]
size = 5

[lock.e2e]
queue = "fifo"
[lock.next-mock-core]
[family.next-employee]
key  = "sha1_12(lower(trim(input)))"   # lock name: next-employee-<key>
name = "next-employee-{key}"

[device.desktop]                       # chosen when the hostname matches
match = "parth-desktop*"
pool.agent.size = 12

[claude]
heavy = ['vitest run', 'playwright test', 'npm run build', 'docker compose up', 'npm run dev']
on_heavy_without_slot = "block"        # block | wrap (rewrite to `slots run test -- …`)
idle_reminder_min = 30

[warn]
long_held_h = 12
idle_min    = 30
wait_min    = 60

[protect]
containers = ["be-postgres", "redis"]
ports      = [5432, 6431]
```

`slots setup` writes this with comments, filling pool sizes from the machine (cores, RAM) as a starting point.

## 4. State layout

Everything lives under one directory, `$SLOTS_HOME` (default `~/.local/state/slots` on both OSes). The tool never deletes outside it.

```
~/.local/state/slots/
  daemon.sock        Unix socket (0600)
  daemon.pid         single-instance flock
  state.db           SQLite: leases, queue, sessions, ports, flags, admin log
  daemon.log         rotating
  cluster/           hub cert/key, token (0600), only when cluster init was run
```

SQLite tables: `lease`, `waiter` (queue position, token), `session` (id, lane, cwd, pid, busy/idle, last_seen), `port` (lane, service, port), `flag` (watcher outputs), `admin_log`, `proc` (pids started through `slots run`, for exact attribution).

## 5. CLI surface

```
slots setup                         guided first run (config + hooks + optional shims)
slots run <res...> [--label L] [--timeout S] -- <cmd>    exit code = cmd's
slots acquire <res> [--lane T] [--wait] [--timeout S] [--session ID] [--pid N]
slots release <res|--all> [--lane T]
slots heartbeat [--session ID]
slots status [--json]               plain text for agents; JSON for tools
slots dash [--once] [--acs|--unicode] [--solid|--transparent]
slots port <lane> <service> [--env] stable port lease; --env prints KEY=PORT lines
slots admin release|dequeue|kill <target>     confirm unless --yes; logged
slots claude install [--user|--project] [--dry-run]
slots claude skill [-o PATH]
slots migrate --from <old root> [--apply]
slots shims install [--dir D]       agent-slot.sh, slot.sh, lib-slot.sh with the old interface
slots cluster init | join <url> | status
slots daemon run|stop|install|uninstall
slots init                          only write the commented example config
```

Resources are named `agent`, `test`, `e2e`, or `next-employee:<email>`. A family member's key is hashed, so the email itself never appears in the name.

## 6. Claude Code hooks

Sources: https://code.claude.com/docs/en/hooks.md, …/settings.md, …/skills.md, …/env-vars.md (checked 2026-11-15). The step 4 tests run against fixture payloads copied from those pages. The docs check came from a documentation-lookup agent, so I re-read the pages before step 4.

**Fields every hook receives on stdin, and which ones we use:** `session_id`, `cwd`, `hook_event_name`, `transcript_path`, `agent_id` (set inside subagents). Hooks run as `slots hook <event>`, read the JSON from stdin, and talk to the daemon.

| Event (matcher) | Extra fields used | What `slots` does | Output |
|---|---|---|---|
| `SessionStart` (no matcher: `startup`, `resume`, `clear`, `compact`, `fork`) | `source` | Registers the session: `session_id`, lane from `cwd`, and the session pid. A repeated `resume`/`compact` for the same id updates it. Writes `SLOTS_SESSION`/`SLOTS_LANE` into `$CLAUDE_ENV_FILE`, so the session's Bash commands and the shims inherit its identity. | `hookSpecificOutput.additionalContext`: a two-line summary (lane, free slots, how to acquire) |
| `UserPromptSubmit` | — | Marks the session busy and sends a heartbeat. | none |
| `PreToolUse` (`Bash`) | `tool_input.command`, `tool_input.run_in_background` | Sends a heartbeat, then classifies the command with the `claude.heavy` patterns. If it's heavy and the session holds no `agent` lease, the action depends on mode. **block** (default): `permissionDecision: "deny"` with a `permissionDecisionReason` like `"Heavy command: hold an agent slot first: slots acquire agent --wait"`. **wrap** (opt-in): `updatedInput.command` set to `slots run test --label "<lane> <cmd>" -- <cmd>`. Commands that already go through `slots run`/`slot.sh` pass. | JSON on stdout, exit 0 |
| `Stop` | — | Marks the session idle and records the time. | none |
| `Notification` (`idle_prompt`) | `notification_type` | If the session has held an `agent` lease with no live lane processes for `idle_reminder_min`, it adds the session to the dashboard's **Needs you** panel. A desktop notification is optional and off by default. There's nothing to inject into the session from this event, so the reminder goes to you. | none |
| `SessionEnd` | `reason` | Releases every lease the session holds and reports what it released, on stderr and in the admin log. | none (can't block) |

Decisions from the docs:

- **SessionEnd has a short budget** (reported as 1.5 s by default, adjustable with `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS`). The hook sends one `release_session` message and exits without waiting for a reply. If the hook doesn't run at all, the session-pid check frees the leases once the session's process exits.
- **The session pid isn't in the payload or the environment.** The hook walks up its parent pids (`ps -o ppid=`) to the first process that is a Claude Code CLI (command `claude`, or the version-number name the reference dashboard already detects) and records that pid with its start time. If none is found, the lease is marked `unverified` (see §1).
- **Wrap mode never returns `permissionDecision: "allow"`,** because that would skip your permission prompt. In auto mode, the docs warn that a rewritten command may be denied when the classifier reviewed only the original. Block stays the default, and the README explains the trade-off.
- **Install:** `slots claude install --user` merges into `~/.claude/settings.json`; `--project` uses `.claude/settings.local.json`, which isn't committed. The settings shape is `hooks.<Event>[] = {matcher?, hooks: [{type: "command", command, timeout}]}`. Install is idempotent: our entries carry a `slots hook` command prefix, so they're found and replaced, and other hooks are left alone. `--dry-run` prints a unified diff. `slots claude uninstall` removes only our entries.
- **Skill:** `slots claude skill` writes `~/.claude/skills/local-slots/SKILL.md` with `name` and `description` frontmatter. The body (limits, resource names, commands, the "when you see X" table) is generated from the config, so it matches the reference skill's content and never drifts.

## 7. Lanes, processes, ports

- **Lane:** the folder under `lanes.root` that contains the session's `cwd`, with the ticket taken from `pattern`.
- **Processes, in order of precision:**
  1. pids started by `slots run`, recorded with their whole tree;
  2. the process tree under each registered Claude Code session pid;
  3. a fallback regex on `cwd`/command line for anything else (on Linux via `/proc/<pid>/cwd`, on macOS via `lsof -d cwd`). Fallback matches are marked `~` in the dashboard.

  Process data comes from `ps -axo pid,ppid,pcpu,rss,lstart,command`, which works on both OSes, so no psutil is needed.
- **Ports:** `slots port` hashes `(lane, service)` into a configured range, then probes for the next port that is both free and unleased, and records it. Ports are always local to one machine, even in cluster mode. `--env` prints a `.env` fragment.

## 8. Watchers

A watcher is a config table with a `kind`, a `trigger` (`every = "30s"`, or `on = "port_up" | "lease_taken" | "flag_changed"`), and its outputs written as flags. There are two built-in kinds:

- `credential`: runs a command; the exit code means valid or not, and an optional `expiry_jq`-style path (a small built-in JSON path, no `jq` needed) gives the expiry time. Used for Vault and AWS SSO.
- `lane_setup`: when a lane's `port` comes up and `artifact` (a path template) is older than the process listening on it, runs `command` in `cwd`, taking `locks` first. It remembers failures and retries. This replaces `signin-watcher.sh` with no tickets or ports in code.

Watchers run inside the daemon. A custom watcher is a Python file with `def check(ctx) -> dict[str, str]`, loaded from a configured path.

## 9. Dashboard: your point of contact

Agents operate `slots` through the CLI, hooks and shims. You operate it through the dashboard, so the dashboard is designed as the one place where you see what's happening and act on it. Plain `slots` with no arguments opens it.

- **Needs you (top, only shown when non-empty).** Everything waiting on a human: `idle?` and `long-held` slots, `unverified` leases, expired credentials (Vault, AWS SSO), a failed lane-setup watcher, a waiter queued longer than `warn.wait_min`, port conflicts, an unreachable hub in cluster mode, and a reminder when the hooks aren't installed or the config changed since `setup`. Each item has its fix on one key.
- **Sessions:** each Claude Code session's lane, busy/idle, time since last activity, the leases it holds, its processes (CPU, RSS, ports), and what it's waiting for. This is the main view, since it answers "which agent is doing what".
- **Resources:** pools, locks and families with their holders, queues and wait times. These are the reference panels, kept.
- **Machine:** load, memory, swap and Docker (read-only), with the reference's history graphs. In cluster mode, a **Devices** row per machine.
- **Activity feed:** a rolling log of acquire, release, wait, block (with the blocked command), watcher result and admin action, so you can see what agents did without reading their transcripts.
- **Actions (y/N confirm each):** release a slot, dequeue, clear a stale holder (refused while its pid is alive), SIGTERM a lane's processes (shows the pids first), move a waiter to the head of a queue, and **change a pool's size live**. The size change is written back to the config file, since only you change limits. Docker containers and the `shared_never_stop` list can never be acted on. Every action goes through the broker's admin API and is logged.
- **Rendering:** the reference's ACS-by-default and Unicode-when-measured glyphs, solid or transparent background, one or two columns depending on width, a detail pane (`d`), and `?` for help. `slots dash --once` prints one frame with no terminal (smoke test, and `slots status` for agents).

Because the dashboard is your interface, it moves up in the build order: step 3 builds the Sessions, Resources and Machine panels plus Needs you, and each later step adds its own items to Needs you and the feed.

## 10. Compatibility

- **Shims:** `agent-slot.sh`, `slot.sh` and `lib-slot.sh` (with `run_in_slot`, `run_exclusive`, `next_employee_lock`) become thin wrappers over `slots`, with the same arguments, output phrases and exit codes as the reference. Golden-output tests compare them against the reference scripts. `TA_EXCLUSIVE_LOCK` is still set for `run_exclusive`.
- **tenantLock:** a ~60-line Node client (`@slots/client`) that connects to the socket, so its lease is `conn`-scoped, keeps extending the test timeout while it waits, and releases on exit.
- **Migration:** `slots migrate --from ~/Documents/dev/interface` reads `.agent-slots/` and `ta-e2e/.locks/*.holder`. Holders whose pid is alive are imported as leases (agent slots, which have no pid, are imported as `unverified` session leases and matched to a live session when one registers in that lane), dead ones are dropped, and the e2e queue order is kept. It prints a table of what it did and changes nothing without `--apply`.

## 11. Testing

- Unit tests: broker state machine, config resolution (including device overrides), name hashing (checked against the reference's `shasum` output).
- Concurrency (real processes): 30 clients racing for a pool of 2; FIFO order under contention; `kill -9` of a holder, of a waiter, and of the daemon in the middle of a queue; session-pid death; opt-in `ttl` expiry; wrong identity releasing; pid reuse.
- Cluster: a hub and two members as three daemons on one machine with separate `SLOTS_HOME`s. Covers a partitioned member losing its shared lease after `grace`, and offline `wait`/`fail`.
- Dashboard: `slots dash --once` with no terminal.
- CI: GitHub Actions on `ubuntu-latest` and `macos-latest`, Python 3.11 and 3.13. The dev config uses a temp root and pools of 2.

## 12. Build notes

- **Step 1 (broker and CLI).** Lease kinds as built: `conn` (held by a connection: `slots run`), `pid` (`slots acquire --pid N`) and `manual` (`slots acquire` with no pid; ends only on release or an admin action). Step 4 adds the session tie: the SessionStart hook records the session's pid, and `acquire` from inside a session becomes a `pid` lease on it. Exit codes: 0 ok, 1 busy or refused, 2 usage or config error, 69 daemon unreachable, 124 timed out; `slots run` otherwise exits with its command's code (128+N for a signal). Admin actions are `slots admin release <res> [--unit N] [--holder LANE]` and `slots admin dequeue <res> --holder LANE`, and both need `--yes` when there's no terminal to ask on.
- **Step 2 (shims, migration, Node client).** `slots shims install --dir D [--only ...] [--force]` writes `agent-slot.sh`, `slot.sh` and `lib-slot.sh`. They exec `slots compat ...`, which reproduces the reference's output phrases and exit codes. A test replays one transcript against both the shim and the original `agent-slot.sh` when `reference/` is present. An existing file is replaced only with `--force` and is kept as `<name>.orig-<time>`. The `[compat]` table names the pools and locks the shims use. Behaviour changes from the reference: `--wait` is event-driven rather than polling every 30 s; a waiter that dies leaves the queue; `e2e-release` also removes a live waiter from the queue; `e2e-acquire` no longer exits silently when the queue file doesn't exist yet (a bug in the reference); `TA_SLOT_COUNT` is read from the config and no longer from the environment. `slots migrate --from <root> [--locks DIR] [--apply]` reads the old layout and imports what it can. Old agent slots and the old e2e lock carry no pid, so they become manual leases. Test-slot and lock holders whose pid is alive become pid leases. Dead ones are dropped. The old e2e queue is reported but not imported, because its waiters are loops polling the old files. Nothing is changed without `--apply`, and old files are never touched. `clients/node` (`slots-client`) replaces `tenantLock.ts`'s `lockf` child with a `slots run` child: `holdLock(name, holder, {onWait, whileWaiting})`, `releaseLocks(holder?)`, and `slots-client/playwright`'s `extendTimeoutWhileWaiting(test)`.
- **Step 3 (dashboard).** Plain `slots` in a terminal, or `slots dash`, opens the live view. `slots dash --once` prints one frame with no terminal, and `slots status` prints the same model as text (`--json` gives broker state; add `--full` for lanes, needs-you and machine). Panels: machine, which shows load, ram, swap and the first two pools with history; **needs you**, which lists idle slots (no lane processes for `warn.idle_min` since the last activity), long-held leases, long waits, thrashing, over-limit pools after a shrink, and a daemon running a different config; lanes, with processes tracked exactly through lease holders' process trees, plus a `~` fallback for processes found by command line or folder (matching `lanes.processes`); resources; activity; and ports and containers (read-only). Keys: enter fixes the selected Needs-you item, `r` releases a lease, `c` clears a lease whose holder is gone, `e` removes a waiter from its queue, `u` moves a waiter to the front, `+`/`-` resizes a pool by editing only that `size` line in the config (the file is parsed before it atomically replaces the original), `K` sends SIGTERM to a lane's processes (never Docker's, a `[protect]` port's or a Claude session's), plus `d`, `?` and `q`. Every action asks y/N and goes through the daemon's admin API, which logs it. The renderer is the reference's: ACS glyphs by default, Unicode only when measured, and a solid or transparent background. The `[shared_never_stop]` table from §3 is named `[protect]`.
- **Step 4 (Claude Code).** `slots claude install|uninstall [--user|--project] [--dry-run]` merges six hooks (SessionStart, UserPromptSubmit, PreToolUse on Bash, Stop, Notification on `idle_prompt`, SessionEnd), each marked `# slots-hook`, so reinstalling is idempotent and other hooks are left alone. The old file is backed up in the state folder. `slots claude skill` generates `local-slots/SKILL.md` from the config. Sessions are registered in the broker with their `claude` process, found by walking up the hook's parent processes; `SLOTS_SESSION_PID` overrides this. A session ends at SessionEnd, or when that process dies, and every lease it holds ends with it. `slots acquire` inside a session (it has `SLOTS_SESSION` from `CLAUDE_ENV_FILE`) makes a pid lease on the session's process. PreToolUse classifies commands with the `[claude]` `heavy`, `stack` and `exempt` regexes and denies heavy work in a lane without an agent slot, explaining how to get one. In `wrap` mode it rewrites heavy commands to `slots run test -- bash -c ...` through `updatedInput`, which replaces the whole tool input, so every field is passed through. It sets no `permissionDecision`, so the user's permission rules still apply; that's why `block` is the default. Hooks fail open and log to `hooks.log`. Verified against the hooks docs on 2026-11-16: `updatedInput` replaces the input; a deny's reason goes to the model; `CLAUDE_ENV_FILE` takes `export` lines run before each Bash command; SessionEnd output is discarded within a 1.5 s budget; hooks run through `sh -c`. In the dashboard, a session's own process tree is shown on the session rather than counted as lane work, and a session that is idle (Stop) while holding a slot past `warn.idle_min` goes to Needs you.
- **Step 5 (ports).** `slots port <lane> <service> [--want N] [--env] [--release]` leases ports, held in the broker and persisted. The first choice is sha1(`lane/service`) mapped into `[ports] range`, so a lane gets the same port again after releasing it. From there the search walks forward past ports that another lane holds, that are in `[protect] ports`, or that a bind test shows in use. `--want` is refused when another lane holds the port. `--env` prints `{SERVICE}_PORT=N` lines for a `.env` fragment. The dashboard shows leases on lanes and in the ports panel, and puts a lease with another lane's process listening on it in Needs you.
- **Step 6 (watchers).** `[watcher.NAME]` tables are checked when the config loads, and the daemon runs them with asyncio (`watchers.Runner`), each in its own task, so a slow or broken watcher never blocks the broker. Triggers: `every`, plus `on` = `start`, `lease:<resource>` (fired on every grant, immediate or queued) and `flag:<watcher>` (when that flag's state changes). Kinds: `credential` (the exit code, then an optional JSON path giving expiry, with `warn_before`); `lane-setup` (for each lane with its port listening, it compares the artifact's mtime with the listening process's start time and runs the command, with `locks` taken through `slots run`, so it queues like anything else; failures are retried and logged in `watchers/<name>-<lane>.log`); and `python` (`check(ctx)` in a thread). Flags are persisted. `slots watchers [--run NAME]` lists or runs them. Warn and bad flags go to Needs you, and the dashboard has a watchers panel. Credential failures are "hot"; per-lane failures are warnings.

