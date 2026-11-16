# slots manual

`slots` keeps many Claude Code sessions (and anything else) from overloading one machine and from
breaking each other's runs. Heavy work takes a slot from a **pool**; one-at-a-time things take a
**lock**. Whoever holds one holds a **lease**, tied to a lane, a session and a process, so a crashed
holder never blocks anyone. Waiters are served first come, first served, and told the moment it's
their turn.

Read it in the terminal with `slots help <topic>`, as a man page with `man slots` (after
`slots man --install`), or on the web at https://parth12358.github.io/Slots/wiki/.

| Topic | What's in it |
|---|---|
| [quickstart](quickstart.md) | Install, first config, the five commands you need |
| [setup](setup.md) | A prompt to paste into Claude Code that sets slots up on a machine |
| [agents](agents.md) | The rules for agents (Claude Code sessions): read this before running anything heavy |
| [concepts](concepts.md) | Pools, locks, families, leases, lanes, the queue, crash safety, identity |
| [claude](claude.md) | Claude Code hooks, the generated skill, sessions in the dashboard |
| [cli](cli.md) | Every command, option and exit code |
| [config](config.md) | Every config setting, with defaults |
| [dashboard](dashboard.md) | The live view: panels, Needs you, keys |
| [compat](compat.md) | agent-slot.sh, slot.sh, lib-slot.sh, migration, the Node client |
| [troubleshooting](troubleshooting.md) | "I see X, what do I do?" |

Built so far: the broker and CLI, the compatibility shims and migration, the dashboard, and the
Claude Code hooks and skill, and port leases. Coming next: watchers (credential freshness, lane
setup), and optional sharing across machines. The design is in
[design](https://parth12358.github.io/Slots/design.html).
