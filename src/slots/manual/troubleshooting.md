# Troubleshooting

| You see | What it means | Do |
|---|---|---|
| `waiting for ...; #2 in line` | everything's taken | wait; it starts by itself |
| `... not free: ...` (exit 1) | no `--wait` was given | run again with `--wait` |
| `timed out waiting` (exit 124) | `--timeout` passed | wait longer, or look at `slots status` |
| `refused: ... held by ...` | it's someone else's | leave it; a person can release it from the dashboard |
| `not inside a lane folder` | the working folder isn't under `lanes.root` | `cd` into your lane, or `--lane <ticket>` |
| `unknown resource 'x' (known: ...)` | not in the config | use one of the names listed |
| `can't reach the slots daemon` (exit 69) | it failed to start | read `~/.local/state/slots/daemon.log` |
| `no config found` | no config file | `slots init`, or set `SLOTS_CONFIG` |
| `socket path too long` | `SLOTS_HOME` is too deep | set `SLOTS_HOME` to a shorter folder |
| Needs you: `idle? X holds agent slot N` | a slot with nothing running | if it's yours, release it; the dashboard can too |
| Needs you: `the daemon uses A, not B` | you changed which config you use | `slots daemon stop`; the next command restarts it with yours |
| a pool shows `over the limit` | you lowered its size | nothing; no new grants until it's under |
| `the daemon restarted and this run's slot was given away` | the daemon was down longer than 10 s | the command keeps running; its slot is no longer counted |

## Looking deeper

```bash
slots status --json --full     # everything, as JSON
slots daemon status            # pid, version, config
tail ~/.local/state/slots/daemon.log
sqlite3 ~/.local/state/slots/state.db 'select row from admin_log order by id desc limit 20'
```

The state folder is the tool's own: stopping the daemon and deleting `state.db` starts over
from nothing (every lease is lost). Nothing outside it is ever deleted by slots.
