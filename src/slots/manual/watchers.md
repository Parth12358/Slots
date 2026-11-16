# Watchers

Small jobs the daemon runs for you, on an interval or when something happens. Each one sets a
flag, ok, warn or bad, which `slots watchers`, `slots status` and the dashboard show. Anything
warn or bad goes to the dashboard's Needs you.

## Credential freshness

Runs a command. Exit code 0 means valid. With `expiry`, a JSON path in the command's output,
it also knows when the credential runs out, and warns `warn_before` that.

```toml
[watcher.vault]
kind = "credential"
command = "vault token lookup -format=json"
expiry = "data.expire_time"
warn_before = "30m"
every = "60s"
env = { VAULT_ADDR = "https://vault.example.com" }

[watcher.aws]
kind = "credential"
command = "aws sts get-caller-identity --profile dev"
every = "5m"
```

## Run a lane's setup when its stack comes up

For each lane whose `port` is listening (a service the lane leased with `slots port`, or a
number): when `artifact` is older than the process listening on that port, or the last run
failed, wait `settle`, then run `command` in `cwd`. It holds `locks` while it runs, waiting its
turn like any other run. With `success`, the output must contain it too. A failure is flagged
and retried on the next round.

```toml
[watcher.signin]
kind = "lane-setup"
port = "ui"                                     # the lane's `slots port <lane> ui`
artifact = "~/harness/.auth/{lane}.json"
command = "npx playwright test --project=setup"
cwd = "~/harness"
env = { TICKET = "{lane}" }
success = " passed"
locks = ["e2e"]
settle = "20s"
every = "30s"
```

Each lane gets its own flag, `signin:<lane>`, and its own log in the state folder's `watchers/`.

## Your own

A Python file with a `check(ctx)` function. `ctx` has `state` (what `slots status --json` shows),
`config` and `lanes`. Return `{"state": "ok" | "warn" | "bad", "text": "..."}`, or a list of
those with a `name` each for several flags.

```toml
[watcher.disk]
kind = "python"
path = "~/.config/slots/disk.py"
every = "5m"
```

## When they run

- `every`: an interval (`30s`, `5m`, `1h`).
- `on`: `start` (when the daemon starts), `lease:<resource>` (each time it's taken), or
  `flag:<watcher>` (when that watcher's flag changes state). One or a list.

`slots watchers` lists them, their flags and when they run next; `slots watchers --run <name>`
runs one now. A watcher that breaks flags itself bad and never stops the daemon. Templates in
`artifact`, `command`, `cwd` and `env`: `{lane}`, `{root}`, `{home}`, `{port}`.
