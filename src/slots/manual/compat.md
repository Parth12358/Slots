# Old scripts, migration, Node

## Shims

`slots shims install --dir <folder>` writes `agent-slot.sh`, `slot.sh` and `lib-slot.sh` with the
old scripts' arguments, output and exit codes, backed by slots. Put them where the old ones were,
so existing scripts and agents keep working. The pools and locks they use are set in `[compat]`.

```bash
agent-slot.sh acquire <ticket> [--wait]     # "<ticket> took slot 3" / "all 7 slots are taken:"
agent-slot.sh release <ticket>
agent-slot.sh status
agent-slot.sh e2e-acquire <ticket> [--wait] # "e2e lock held by X since T; queue: ..."
agent-slot.sh e2e-release <ticket>          # also leaves the queue
slot.sh [--label "<ticket> vitest"] -- <command>   # "waiting for a test slot (5 in use: ...)"
source lib-slot.sh; run_in_slot <label> <cmd...>; run_exclusive <lock> <what> <label> <cmd...>
next_employee_lock <email>                  # next-employee-<sha1-12>, as lib/env.ts
```

Differences from the old scripts: waits are woken at once instead of polling every 30 s; a waiter
that dies leaves the queue; `e2e-release` also removes a live waiter of that ticket; limits come
from the config, not `SLOTS` or `TA_SLOT_COUNT`; `run_exclusive` still sets `TA_EXCLUSIVE_LOCK`.

## Migration

```bash
slots migrate --from ~/old/root                # shows what it would do; changes nothing
slots migrate --from ~/old/root --apply
```

It reads `.agent-slots/slot-<n>`, `.agent-slots/e2e`, `.agent-slots/e2e-queue` and
`<root>/ta-e2e/.locks/*.lock.holder` (`--locks` for another folder):

- old agent slots and the e2e lock become `no pid` leases (the old files record no pid);
- test-slot and lock holders whose pid is alive become pid leases, which end with that process;
- dead holders are dropped; the old queue is reported (its waiters poll the old files, so rerun
  `agent-slot.sh e2e-acquire <ticket> --wait` in the order shown);
- the old files are never edited or deleted. Running it twice imports nothing twice.

## Node (Playwright)

`clients/node` (`slots-client`): hold a lock from a test, released when the process exits.

```js
import { holdLock, releaseLocks } from 'slots-client';
import { extendTimeoutWhileWaiting } from 'slots-client/playwright';
import { test } from '@playwright/test';

await holdLock('next-mock-core', holder, { whileWaiting: extendTimeoutWhileWaiting(test) });
// ...
await releaseLocks(holder);
```

`SLOTS_BIN` says how to run slots (default `slots`).
