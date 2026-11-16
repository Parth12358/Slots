---
title: slots
---

# slots

`slots` keeps many Claude Code sessions from overloading one machine and from breaking each
other's test runs. Pools and locks are declared in one config file, holders are leases tied to
a lane, a session and a pid, a crashed holder never blocks anyone, and waiters are served first
come, first served.

## Install

Needs Python 3.11+ on macOS or Linux. No root, no services to install.

```bash
uv tool install git+https://github.com/Parth12358/Slots@prod
slots init            # writes ~/.config/slots/config.toml with comments
```

Release wheels are on the [releases page](https://github.com/Parth12358/Slots/releases).

## Quick start

```bash
slots run test -- npx vitest run     # hold a test slot for one command; exits with its code
slots acquire agent --wait           # hold an agent slot across commands
slots release agent
slots status                         # who holds and awaits what (--json for tools)
```

## Coming from agent-slot.sh / slot.sh

```bash
slots migrate --from ~/old/root            # shows what would be imported; changes nothing
slots migrate --from ~/old/root --apply    # adopts live holders; old files are left alone
slots shims install --dir ~/old/root       # agent-slot.sh with the same arguments and output
```

## Manual

The full manual is the [wiki](wiki/index.md). Every page is also in the terminal, offline:
`slots help <topic>`, and `slots man --install` then `man slots`.

| | |
|---|---|
| [Quick start](wiki/quickstart.md) | install, first config, the five commands |
| [For agents](wiki/agents.md) | the rules for Claude Code sessions: read before running anything heavy |
| [Concepts](wiki/concepts.md) | pools, locks, leases, lanes, the queue, crash safety |
| [Commands](wiki/cli.md) | every command, option and exit code |
| [Config](wiki/config.md) | every setting, with defaults |
| [Dashboard](wiki/dashboard.md) | panels, Needs you, keys |
| [Old scripts](wiki/compat.md) | shims, migration, the Node client |
| [Troubleshooting](wiki/troubleshooting.md) | "I see X, what do I do?" |

**Agents setting up:** fetch [wiki/all.txt](wiki/all.txt), the whole manual as plain text in one
file, or start from [llms.txt](llms.txt).

## More

- [Design](design.md): the broker, its failure modes, state, CLI, and the Claude Code hook plan.
- [Source](https://github.com/Parth12358/Slots)
