# Concepts

## Resources

All declared in the [config](config.md), never in code.

- **Pool**: a counting semaphore of N slots, e.g. `agent` (7) or `test` (5). Slots are numbered
  1..N; a holder gets the lowest free one.
- **Lock**: a pool of one, e.g. `e2e` or `next-mock-core`.
- **Family**: locks made on demand from a key. `employee:alice@example.com` takes the lock
  `employee-<first 12 hex of sha1 of the trimmed, lowercased key>`, so the key itself is never
  stored. A family lock can also be named by its full name.

## Leases

A holder holds a **lease**, owned by:

- the **lane** (the ticket folder it works in),
- the **session** (the Claude Code session id, when there is one; `$SLOTS_SESSION`),
- the **pid** that holds it.

A lease is one of three kinds, by how it ends:

| Kind | Made by | Ends when |
|---|---|---|
| `run` (connection) | `slots run` | the command ends, or `slots run` is killed, even with `kill -9` |
| `pid` | `slots acquire --pid N` | that process exits (checked every 2 s; a reused pid doesn't count) |
| `no pid` (manual) | `slots acquire` | it's released, by its owner or from the dashboard |

There's no inactivity timeout unless a pool sets `ttl`; long or idle leases are flagged in the
dashboard's **Needs you** panel instead, for a person to decide.

## Only the holder releases

`slots release` releases the caller's own leases: the same lane, and the same session when both
name one. Releasing someone else's lock is refused with a message saying who holds it. Releasing
a pool you hold nothing of is a no-op. A person can release anything from the dashboard or with
`slots admin`, and every such action is logged.

## The queue

There is one first-come-first-served order across every resource. A request for several resources
at once (`slots run test e2e -- ...`) is granted all together or not at all, and while it waits it
reserves its place on each, so nobody behind it can starve it and no two waiters can deadlock.
Waiting is event-driven: the daemon tells a waiter the moment it's granted.

## Crash safety

- A holder that dies frees its lease: at once for `slots run`, within 2 s for a pid lease.
- A waiter that dies leaves the queue at once.
- The daemon keeps its state in SQLite. If it's killed, the next `slots` command starts a new one,
  which reloads every lease. Running `slots run` commands reconnect and keep their slots, and
  waiters keep their place; anything not reclaimed within 10 s is dropped.

## The daemon

One per machine, on a Unix socket in the state folder (`$SLOTS_HOME`, default
`~/.local/state/slots`). It starts on demand, reloads the config when the file changes, and
never needs to be cleaned up by hand. `slots daemon status|stop`.

## Lanes and processes

A lane's processes are found exactly where possible (the process trees of its leases' holders)
and otherwise by folder: a process whose command line or working folder is inside the lane and
whose command matches `lanes.processes`. The dashboard marks the second kind with `~`.
