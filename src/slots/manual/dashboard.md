# Dashboard

`slots` (in a terminal) or `slots dash`. It's the place to see what every session is doing and to
fix what needs a person. `slots dash --once` prints the same view as text; `slots status` too.

## Panels

- **machine**: load (with a graph), ram, swap, and the first two pools, with recent history.
- **needs you** (only when something does): an idle slot (its lane has had no processes since
  `warn.idle_min`), a long-held lease, a long wait, a thrashing machine, a pool over its limit
  after you lowered it, a daemon running a different config. Select one and press enter to fix it.
- **lanes**: each lane with what it holds, its processes (CPU, memory, count; `~` when some were
  found by folder rather than tracked), its ports, and what it's waiting for.
- **resources**: every pool and lock, with holders (`run`, `pid`, `no pid`), their age, and the
  queue in order.
- **activity**: recent takes, releases, waits, timeouts and admin actions.
- **ports & containers**: dev listeners and running containers. Read-only.
- **detail** (`d`, on terminals 30+ rows tall): the selected row's processes and facts.

## Keys

| Key | On | Does |
|---|---|---|
| j/k, arrows | | move |
| enter | a Needs-you item | its fix (release, or move to front) |
| r | a holder row | release that lease (its processes keep running) |
| c | a holder row | clear a lease whose holder process is gone (refused while it's alive) |
| e | a waiting row | remove it from the queue |
| u | a waiting row | move it to the front |
| + / - | a pool row | grow or shrink the pool: edits only that `size` line in the config file |
| K | a lane row | SIGTERM the lane's processes, after showing them (never Docker, protected ports or Claude sessions) |
| d | | detail pane |
| ? | | help |
| q | | quit |

Every action asks y/N first and is logged by the daemon.

## Display

`--acs` (default) uses line drawing and coloured spaces, which render right everywhere.
`--unicode` uses rounded corners and block glyphs, but only if a check at startup shows the
terminal draws them one column wide. `--solid` (default) paints a dark background;
`--transparent` keeps yours. Two columns from 140 wide; the machine graph from 30 tall.
