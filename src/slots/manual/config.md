# Config

One TOML file. Found via `--config`, then `$SLOTS_CONFIG`, then `./.slots.toml` (searched upward
from the working folder), then `~/.config/slots/config.toml`. `slots init` writes a commented
example. The daemon reloads the file within 2 s of a change; a file that doesn't parse is ignored
(the old one stays in use) and logged.

## [lanes]

| Key | Default | Meaning |
|---|---|---|
| `root` | none | the folder holding one folder per lane |
| `pattern` | `^(?P<ticket>.+)$` | regex on a lane folder's name; the lane id is the `ticket` group, or the whole match. Folders that don't match aren't lanes. |
| `processes` | `\b(node\|vitest\|playwright\|tsx)\b` | processes started outside slots count as a lane's when their command line or folder is in the lane and their command matches this |

## [pool.NAME]

| Key | Default | Meaning |
|---|---|---|
| `size` | required | how many holders at once (1 or more) |
| `hold` | `"command"` | `"session"` for slots held across a session (`acquire`), `"command"` for per-command (`run`); guides the hooks and the skill |
| `ttl` | off | e.g. `"8h"`: end a lease after this long with no activity. Off by default: long leases are flagged, not taken away. |

## [lock.NAME]

A pool of one. Takes `hold` and `ttl` like a pool, and `queue = "fifo"` (the only order).

## [family.NAME]

Locks made on demand: `NAME:<key>`.

| Key | Default | Meaning |
|---|---|---|
| `name` | `"NAME-{key}"` | the lock name; must contain `{key}` |
| `key` | `"sha1-12"` | `"sha1-12"`: the first 12 hex of sha1 of the key; `"raw"`: the key itself |
| `normalize` | `["trim", "lower"]` | applied to the key first |

## [device.NAME]

Overrides for one machine, so one file serves several. Chosen when the hostname matches `match`
(a glob; default the table's name), or by `$SLOTS_DEVICE`. Any setting can be overridden with a
dotted key, e.g. `pool.agent.size = 12`.

## [warn]

When the dashboard's Needs you panel flags something.

| Key | Default | Meaning |
|---|---|---|
| `long_held_h` | 12 | a lease held this many hours |
| `idle_min` | 30 | a held slot whose lane has had no processes (and no activity) for this many minutes |
| `wait_min` | 60 | someone waiting this many minutes |

## [protect]

Shared things nothing in slots ever stops. Docker's own processes and Claude Code sessions are
always spared.

| Key | Default | Meaning |
|---|---|---|
| `containers` | `[]` | container names (shown, never touched) |
| `ports` | `[]` | processes listening on these ports are never sent a signal |

## [compat]

What the old-script shims map onto ([compat](compat.md)).

| Key | Default |
|---|---|
| `agent_pool` | `"agent"` |
| `test_pool` | `"test"` |
| `e2e_lock` | `"e2e"` |
| `employee_family` | `"next-employee"` |
| `undeclared_locks` | `false`: when true, a lock name that isn't declared is made on demand (for `run_exclusive`) |

## [cluster]

`enabled = false`. Sharing chosen resources across machines is designed but not built yet.

## Example

```toml
[lanes]
root = "~/dev/lanes"
pattern = '^(?P<ticket>\d+)$'

[pool.agent]
size = 7
hold = "session"

[pool.test]
size = 5

[lock.e2e]

[family.employee]
name = "employee-{key}"

[device.desktop]
match = "my-desktop*"
pool.agent.size = 12

[protect]
containers = ["postgres"]
ports = [5432]
```
