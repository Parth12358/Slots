# slots

Run twenty Claude Code sessions on one laptop without it falling over.

`slots` is a small local coordinator for heavy work: dev stacks, builds, full test suites, e2e
runs. You declare what the machine can take ("7 sessions may run stacks, 5 test suites at once,
one e2e run at a time") and every session takes turns. A crashed or killed session never leaves
anything stuck, waiters are served in arrival order and told the moment it's their turn, and a
dashboard shows you what every session is doing.

```bash
slots run test -- npx vitest run     # waits for a test slot, runs, frees it; exits with the command's code
slots acquire agent --wait           # hold a slot across commands (a session running its stack)
slots release agent
slots                                # the live dashboard
```

- **Manual:** https://parth12358.github.io/Slots/wiki/, or offline: `slots help`, `man slots`
- **For agents:** `slots help agents`, or the whole manual as text:
  https://parth12358.github.io/Slots/wiki/all.txt
- **Design:** [docs/design.md](docs/design.md)

## Install

Python 3.11+ on macOS or Linux. Nothing else: no root, no services, no dependencies.

```bash
uv tool install git+https://github.com/Parth12358/Slots@prod     # or: pipx install git+https://...
slots init                          # writes ~/.config/slots/config.toml, with comments
slots claude install --dry-run      # see what it adds to Claude Code's settings
slots claude install && slots claude skill
slots man --install                 # optional: man slots
```

The daemon starts by itself on first use.

## What you get

| | |
|---|---|
| **Pools and locks from config** | `[pool.agent] size = 7`, `[lock.e2e]`, lock families keyed by a hash (`employee:<email>`). Per-machine overrides in the same file. Edited live; the daemon reloads it. |
| **Leases that can't get stuck** | A holder is a lane, a session and a process. `slots run` frees its slot the instant the command ends or is `kill -9`'d; a slot held by a session ends with that session. The daemon itself can be killed and nothing is lost. |
| **A fair queue** | First come, first served across every resource, multi-resource requests granted together, no polling. |
| **Only the holder releases** | Another session can't release your lock; you can, from the dashboard, with a confirm, and it's logged. |
| **Claude Code hooks** | Sessions register themselves, heavy Bash commands without a slot are stopped with the exact command to get one (or wrapped in a test slot), and ending a session gives everything back. |
| **A dashboard** | Sessions (busy or idle, what they hold and run), a "needs you" list (idle slots, long waits, a thrashing machine) with one-key fixes, lanes, resources, activity, ports, load and memory. |
| **Drop-in for the old scripts** | `agent-slot.sh`, `slot.sh` and `lib-slot.sh` shims with the same arguments, output and exit codes, and `slots migrate` to adopt what they hold. A Node client for Playwright tests. |

## Status

Built: the broker and CLI, compatibility shims and migration, the dashboard, Claude Code hooks
and the generated skill. Next: port leases, watchers (credential freshness, lane setup), and
optional sharing across machines (off by default).

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .     # stdlib only; ~2 minutes
SLOTS_HOME=/tmp/slots-dev SLOTS_CONFIG=examples/dev.toml PYTHONPATH=src python3 -m slots status
```

CI runs the tests on macOS and Linux for every push and pull request. A push to `prod` also
publishes a release (when the version in `pyproject.toml` is new) and the site.

## License

GPL-2.0-only. See [LICENSE](LICENSE).
