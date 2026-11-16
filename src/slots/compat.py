"""The old scripts' interface on top of the broker: `slots compat <name> ...`.

The shell shims (agent-slot.sh, slot.sh, lib-slot.sh) exec these, so existing
scripts and agents keep working with the same arguments, output phrases and exit
codes as the reference scripts. Which pools and locks they map onto is set in the
config's [compat] table.
"""
from __future__ import annotations

import time

from slots import cli
from slots.cli import EXIT_UNAVAILABLE, Ctx, err

AGENT_USAGE = "usage: agent-slot.sh acquire|release|e2e-acquire|e2e-release <ticket> [--wait] | status"


def utc_minute(ts: float) -> str:
    """agent-slot.sh's `date -u +%Y-%m-%dT%H:%MZ`."""
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts))


def utc_second(ts: float) -> str:
    """lib-slot.sh's holder time, `date -u +%H:%M:%SZ`."""
    return time.strftime("%H:%M:%SZ", time.gmtime(ts))


def holder_text(h: dict) -> str:
    """A .holder file's line: "<label> (pid N, since HH:MM:SSZ)"."""
    o = h["owner"]
    return f"{o.get('label') or o.get('lane') or 'someone'} (pid {o.get('pid')}, since {utc_second(h['since'])})"


def resource_state(ctx: Ctx, name: str) -> dict:
    reply = cli.ctx_call(ctx, {"op": "status"})
    if reply is None:
        raise SystemExit(EXIT_UNAVAILABLE)
    for r in reply["state"]["resources"]:
        if r["name"] == name:
            return r
    size = ctx.cfg.resources[name].size if name in ctx.cfg.resources else 1
    return {"name": name, "size": size, "holders": [], "queue": [], "in_use": 0}


def owner_for(ticket: str) -> dict:
    import os
    return {"lane": ticket, "label": ticket, "session": os.environ.get("SLOTS_SESSION")}


def failed(ev: str, msg: dict) -> int:
    err(f"slots: {msg.get('message', ev)}")
    return EXIT_UNAVAILABLE


# ---- agent-slot.sh -------------------------------------------------------------------------------

def agent_slot(ctx: Ctx, argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "status":
        return agent_status(ctx)
    if cmd not in ("acquire", "release", "e2e-acquire", "e2e-release"):
        err(AGENT_USAGE)
        return 2
    if len(argv) < 2 or not argv[1]:
        err("agent-slot.sh: 2: ticket")   # bash's ${2:?ticket} under set -u
        return 1
    ticket, wait = argv[1], len(argv) > 2 and argv[2] == "--wait"
    pool, e2e = ctx.cfg.compat["agent_pool"], ctx.cfg.compat["e2e_lock"]

    if cmd == "acquire":
        ev, msg = cli.hold(ctx, [pool], owner_for(ticket), wait=wait, on_queued=lambda m: None)
        if ev in ("granted", "already"):
            unit = msg["leases"][0]["unit"]
            print(f"{ticket} already holds slot {unit}" if ev == "already" else f"{ticket} took slot {unit}")
            return 0
        if ev == "busy":
            print(f"all {ctx.cfg.resources[pool].size} slots are taken:")
            agent_status(ctx, e2e_lines=False)
            return 1
        return failed(ev, msg)

    if cmd == "release":
        reply = cli.ctx_call(ctx, {"op": "release", "resources": [pool], "owner": owner_for(ticket)})
        if reply is None:
            return EXIT_UNAVAILABLE
        if reply["ev"] == "refused":
            err(f"refused: {ticket}'s slot belongs to another session; {reply['message']}")
            return 1
        if not reply.get("leases"):
            print(f"{ticket} holds no slot")
        else:
            print(f"{ticket} released slot {reply['leases'][0]['unit']}")
        return 0

    if cmd == "e2e-acquire":
        ev, msg = cli.hold(ctx, [e2e], owner_for(ticket), wait=wait, on_queued=lambda m: None)
        if ev == "already":
            print(f"{ticket} already holds the e2e lock")
            return 0
        if ev == "granted":
            print(f"{ticket} took the e2e lock")
            return 0
        if ev == "busy":
            r = resource_state(ctx, e2e)
            h = r["holders"][0] if r["holders"] else None
            owner = h["owner"].get("lane") or "" if h else ""
            since = utc_minute(h["since"]) if h else ""
            queue = "".join(f"{w['owner'].get('lane')} " for w in r["queue"])
            print(f"e2e lock held by {owner} since {since}; queue: {queue}")
            return 1
        if ev == "dequeued":
            print(f"{ticket} left the e2e queue")
            return 1
        return failed(ev, msg)

    # e2e-release: also leaves the queue, so a lane that stops waiting never blocks the head.
    reply = cli.ctx_call(ctx, {"op": "release", "resources": [e2e], "owner": owner_for(ticket), "leave_queue": True})
    if reply is None:
        return EXIT_UNAVAILABLE
    if reply["ev"] == "released" and reply.get("leases"):
        print(f"{ticket} released the e2e lock")
    else:
        print(f"{ticket} doesn't hold the e2e lock (left the queue)")
    return 0


def agent_status(ctx: Ctx, e2e_lines: bool = True) -> int:
    pool, e2e = ctx.cfg.compat["agent_pool"], ctx.cfg.compat["e2e_lock"]
    r = resource_state(ctx, pool)
    by_unit = {h["unit"]: h for h in r["holders"]}
    for i in range(1, max([r["size"], *by_unit]) + 1):
        h = by_unit.get(i)
        print(f"slot {i}: {h['owner'].get('lane')} since {utc_minute(h['since'])}" if h else f"slot {i}: free")
    if e2e_lines:
        r = resource_state(ctx, e2e)
        if r["holders"]:
            h = r["holders"][0]
            print(f"e2e: {h['owner'].get('lane')} since {utc_minute(h['since'])}")
        else:
            print("e2e: free")
        print("e2e queue: " + "".join(f"{w['owner'].get('lane')} " for w in r["queue"]))
    return 0


# ---- lib-slot.sh ---------------------------------------------------------------------------------

def run_in_slot(ctx: Ctx, argv: list[str], command: list[str]) -> int:
    if not argv or not command:
        err("usage: run_in_slot <label> <command...>")
        return 2
    label, pool = argv[0], ctx.cfg.compat["test_pool"]
    size = ctx.cfg.resources[pool].size
    said = False

    def on_queued(msg: dict):
        nonlocal said
        if not said:
            holders = ";".join(holder_text(h) for h in msg["holders"])
            err(f"waiting for a test slot ({size} in use: {holders})")
            said = True

    def on_failure(ev: str, msg: dict) -> int:
        err(f"slot lock failed (slots: {msg.get('message', ev)})")
        return EXIT_UNAVAILABLE

    return cli.run_holding(ctx, [pool], label, command, quiet=True, on_queued=on_queued, on_failure=on_failure)


def run_exclusive(ctx: Ctx, argv: list[str], command: list[str]) -> int:
    if len(argv) < 3 or not command:
        err("usage: run_exclusive <lock> <what> <label> <command...>")
        return 2
    name, what, label = argv[:3]
    said = False

    def on_queued(msg: dict):
        nonlocal said
        if not said:
            holder = "; ".join(holder_text(h) for h in msg["holders"]) or "another run"
            err(f"waiting for {what} (held by {holder})")
            said = True

    def on_failure(ev: str, msg: dict) -> int:
        err(f"{what} lock failed (slots: {msg.get('message', ev)})")
        return EXIT_UNAVAILABLE

    # The command learns it holds the lock (lib/env.ts nextLockMissing).
    return cli.run_holding(ctx, [name], label, command, quiet=True, on_queued=on_queued,
                           on_failure=on_failure, extra_env={"TA_EXCLUSIVE_LOCK": name})


def employee_lock(ctx: Ctx, argv: list[str]) -> int:
    fam = ctx.cfg.families.get(ctx.cfg.compat["employee_family"])
    if fam is None:
        err(f"slots: no [family.{ctx.cfg.compat['employee_family']}] in the config")
        return 2
    print(fam.lock_name(argv[0] if argv else ""))
    return 0


def slot_holders(ctx: Ctx, argv: list[str]) -> int:
    r = resource_state(ctx, ctx.cfg.compat["test_pool"])
    print(";".join(holder_text(h) for h in r["holders"]))
    return 0


def slot_count(ctx: Ctx, argv: list[str]) -> int:
    print(ctx.cfg.resources[ctx.cfg.compat["test_pool"]].size)
    return 0


def main(ctx: Ctx, argv: list[str], command: list[str]) -> int:
    name, rest = (argv[0], argv[1:]) if argv else ("", [])
    if name == "agent-slot":
        return agent_slot(ctx, rest)
    if name == "run-in-slot":
        return run_in_slot(ctx, rest, command)
    if name == "run-exclusive":
        return run_exclusive(ctx, rest, command)
    handlers = {"employee-lock": employee_lock, "slot-holders": slot_holders, "slot-count": slot_count}
    if name in handlers:
        return handlers[name](ctx, rest)
    err("usage: slots compat agent-slot|run-in-slot|run-exclusive|employee-lock|slot-holders|slot-count ...")
    return 2
