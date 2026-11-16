# For agents

You share this machine with other sessions. `slots` decides who runs heavy work when. Follow these
rules; the limits exist because more at once makes the machine swap, stall or crash, or breaks
another lane's test run.

## The rules

1. **Hold an agent slot before you start anything that runs locally**: a service stack, a dev
   server, a build, a full test suite, an e2e run. Reading and editing code, and running one small
   test file, need no slot.
   `slots acquire agent --wait` (from inside your lane's folder).
2. **Run each heavy command in a test slot**: `slots run test -- <command>`. It waits for a free
   slot, runs the command, frees the slot when it ends, and exits with the command's exit code.
3. **One-at-a-time things take their lock around the run only**, e.g.
   `slots run e2e -- <e2e command>` or `slots run test e2e -- <command>` for both at once.
4. **Release as soon as your processes have stopped**: `slots release agent`. Don't hold a slot
   while you wait for CI, a review, a person or another agent. Acquire again when you're ready.
5. **When everything is taken, wait** (`--wait`, or just `slots run`, which waits by default).
   Never start heavy work without a slot because "it's quick", and never change a limit; only
   the person who owns the machine does that.
6. **Never touch another lane's slot, lock, processes or ports.** `slots release` only releases
   your own; if something looks stuck, report it with the output of `slots status`, don't clear it.
7. **When you're paused, stopped or done**, stop your processes and `slots release --all`.

## Which lane am I?

The lane is the folder directly under the configured root that your working directory is in
(e.g. `~/dev/lanes/10526/...` is lane `10526`). Outside a lane, pass `--lane <ticket>` or set
`SLOTS_LANE`. `slots status` shows what you hold.

## What the messages mean

| You see | Do |
|---|---|
| `waiting for agent (7/7 in use: ...); #3 in line` | Wait. You're told when you're next and you start by yourself. |
| `agent not free: ...` (exit 1) | You didn't pass `--wait`. Run it again with `--wait`. |
| `timed out waiting` (exit 124) | You passed `--timeout`. Wait longer, or report it with `slots status`. |
| `refused: ... doesn't hold it; held by ...` | It isn't yours. Leave it alone. |
| `... holds nothing to release` | Nothing to do; you held nothing. |
| `can't reach the slots daemon` (exit 69) | Report it; don't work around it. |
| A Bash call blocked: "holds no agent slot" | `slots acquire agent --wait`, then run it again (heavy ones as `slots run test -- ...`). |

Full details: [cli](cli.md), [concepts](concepts.md), [troubleshooting](troubleshooting.md).
