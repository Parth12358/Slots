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

## More

- [Design](design.md): the broker, its failure modes, state, CLI, and the Claude Code hook plan.
- [Source](https://github.com/Parth12358/Slots)
