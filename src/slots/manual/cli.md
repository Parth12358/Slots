# Commands

Every command takes `--config <file>` (see [config](config.md) for how the file is found).
Resources are named as in the config: `agent`, `e2e`, or `<family>:<key>`.

## slots run

```
slots run <resource...> [--label L] [--timeout S] [--no-wait] [-q] [--lane T] [--session ID] -- <command...>
```

Waits for one unit of each resource (all at once), runs the command holding them, and releases
them when it ends. Exits with the command's exit code (128+N if a signal ended it; 127 if it
wasn't found). While waiting it prints who it's waiting on, once, and again when it's next.
SIGTERM and SIGHUP are passed on to the command; Ctrl-C reaches it through the terminal.
The command gets `SLOTS_LEASES` (e.g. `test#2`) and `SLOTS_RUN_TOKEN` in its environment.

- `--label L`: how the holder is shown (default `<lane> <command name>`).
- `--timeout S`: give up waiting after S seconds (exit 124).
- `--no-wait`: fail at once if not free (exit 1).
- `-q`, `--quiet`: no progress lines.

## slots acquire

```
slots acquire <resource...> [--wait] [--timeout S] [--pid N] [--label L] [--lane T] [--session ID]
```

Takes leases that outlive the command: with `--pid N` they end when process N exits; without it
they last until released. A lane that already holds the resource gets "already holds" (exit 0).
Without `--wait`, exits 1 if it isn't free. Prints `<lane> took agent slot 3`.

## slots release

```
slots release <resource...> | --all  [--lane T] [--session ID]
```

Releases your own leases. Refused (exit 1) when someone else holds the lock you named. Prints
`<lane> released agent slot 3`, or `<lane> holds nothing to release`.

## slots status

```
slots status [--json [--full]]
```

One snapshot as plain text: summary, Needs you, lanes, every resource with holders and queue,
recent activity, ports, containers, machine. `--json` prints the broker's state (fast, no process
scan); `--json --full` adds lanes, needs and machine.

## slots dash

```
slots dash [--once] [--acs | --unicode] [--solid | --transparent]
slots                                     # the same, in a terminal
```

The live dashboard; see [dashboard](dashboard.md). `--once` prints one frame as text without a
terminal. `SLOTS_GLYPHS=acs|unicode` and `SLOTS_BG=solid|transparent` set the defaults.

## slots admin

```
slots admin release <resource> [--unit N] [--holder LANE] [--reason TEXT] [--yes]
slots admin dequeue <resource> --holder LANE [--yes]
```

Act on someone else's lease or wait. Asks first (needs `--yes` without a terminal); logged.

## slots shims install

```
slots shims install --dir DIR [--only agent-slot.sh,slot.sh,lib-slot.sh] [--force]
```

Writes the compatibility scripts; see [compat](compat.md). An existing file is only replaced
with `--force`, and is kept as `<name>.orig-<time>`.

## slots migrate

```
slots migrate --from OLD_ROOT [--locks DIR] [--apply]
```

Reads the old scripts' files and shows what it would import; changes nothing without `--apply`.
See [compat](compat.md).

## slots init

```
slots init [--path FILE] [--force]
```

Writes a commented example config (default `~/.config/slots/config.toml`).

## slots daemon

```
slots daemon status | stop | run
```

The background broker. It starts by itself; `stop` ends it (the next command starts a new one,
and running commands keep their slots). `run` runs it in the foreground.

## slots help

```
slots help [topic | command]
```

Lists the topics, or prints one (`slots help agents`), or one command's section here
(`slots help run`). The same pages are on the web at https://parth12358.github.io/Slots/wiki/.

## slots man

```
slots man [--install [--dir DIR]]
```

Prints the whole manual as a man page. `--install` writes it to `~/.local/share/man/man1/slots.1`
(or under `--dir DIR`), where `man slots` finds it when `~/.local/bin` is on your PATH.

## slots compat

Used by the shims (`agent-slot`, `run-in-slot`, `run-exclusive`, `employee-lock`, `slot-holders`,
`slot-count`); takes the old scripts' arguments verbatim. Not meant to be run by hand.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | not free (without `--wait`), or a release refused, or dequeued by an admin |
| 2 | usage or config error |
| 69 | the daemon can't be reached or started |
| 124 | timed out waiting |
| other | `slots run`: the command's own exit code |

## Environment

| Variable | Meaning |
|---|---|
| `SLOTS_CONFIG` | the config file |
| `SLOTS_HOME` | the state folder (default `~/.local/state/slots`) |
| `SLOTS_LANE` | the lane, when the working folder doesn't say |
| `SLOTS_SESSION` | the Claude Code session id |
| `SLOTS_DEVICE` | which `[device.<name>]` override to use |
| `SLOTS_GLYPHS`, `SLOTS_BG` | dashboard defaults |
