"""The `slots` command."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from importlib import resources as pkg_resources
from pathlib import Path

from slots import __version__, config, fmt
from slots.client import Client, Unavailable
from slots.paths import Paths

EXIT_BUSY = 1          # not granted (no --wait), or a release refused
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 69  # EX_UNAVAILABLE: the daemon can't be reached or started
EXIT_TIMEOUT = 124     # as timeout(1)
RECONNECT_FOR = 15.0   # how long a client keeps trying to reach a restarted daemon


def err(msg: str):
    print(msg, file=sys.stderr, flush=True)


class Ctx:
    def __init__(self, args):
        self.args = args
        self.paths = Paths.default()
        self.config_path = config.find_config(getattr(args, "config", None))
        self._cfg = None

    @property
    def cfg(self) -> config.Config:
        if self._cfg is None:
            self._cfg = config.load(self.config_path)
        return self._cfg

    def client(self) -> Client:
        return Client(self.paths, self.config_path)

    def owner(self, label: str | None = None, pid: int | None = None) -> dict:
        a = self.args
        lane = getattr(a, "lane", None) or os.environ.get("SLOTS_LANE") or self.cfg.lane_of(os.getcwd())
        session = getattr(a, "session", None) or os.environ.get("SLOTS_SESSION")
        return {"lane": lane, "session": session, "pid": pid, "label": label or lane}


# ---- run / acquire -------------------------------------------------------------------------------

def wait_for_grant(c: Client, req: dict, quiet: bool = False) -> tuple[str, dict]:
    """Send an acquire and follow it to the end, reconnecting if the daemon restarts.
    Returns ("granted"|"already"|"busy"|"timeout"|"error"|"dequeued", last message)."""
    said = None
    deadline = time.monotonic() + req["timeout"] if req.get("timeout") else None
    while True:
        try:
            c.send(req)
            while True:
                msg = c.recv()
                if msg is None:
                    break   # daemon went away: reconnect and ask again with the same token
                ev = msg["ev"]
                if ev == "queued":
                    line = fmt.waiting_line(msg)
                    if not quiet and line != said and (said is None or msg["position"] == 1):
                        err(line)
                    said = line
                    continue
                return ev, msg
        except (BrokenPipeError, ConnectionError):
            pass
        if not reconnect(c):
            return "error", {"message": "lost the slots daemon and couldn't restart it"}
        if deadline is not None:
            req = {**req, "timeout": max(0.1, deadline - time.monotonic())}


def reconnect(c: Client) -> bool:
    until = time.monotonic() + RECONNECT_FOR
    while time.monotonic() < until:
        try:
            c.connect()
            return True
        except Unavailable:
            time.sleep(0.2)
    return False


def lease_names(leases: list[dict]) -> str:
    return ", ".join(f"{l['resource']} slot {l['unit']}" if l.get("pool") else l["resource"] for l in leases)


def describe_busy(msg: dict) -> str:
    held = "; ".join(f"{h['resource']} #{h['unit']}: {fmt.holder(h)}" for h in msg.get("holders", []))
    queue = ", ".join(fmt.who(o) for o in msg.get("queue", []))
    text = f"{', '.join(msg.get('resources', []))} not free: {held or 'reserved for earlier waiters'}"
    return text + (f"; waiting: {queue}" if queue else "")


def cmd_run(ctx: Ctx) -> int:
    a = ctx.args
    command = a.command
    if not command:
        err("usage: slots run <resource...> [--label L] [--timeout S] -- <command...>")
        return EXIT_USAGE
    label = a.label or " ".join(x for x in (ctx.owner()["lane"], Path(command[0]).name) if x)
    token = secrets.token_hex(8)
    req = {"op": "acquire", "resources": a.resources, "owner": ctx.owner(label, os.getpid()),
           "token": token, "wait": not a.no_wait, "timeout": a.timeout}
    c = ctx.client()
    try:
        c.connect()
    except Unavailable as e:
        err(f"slots: {e}")
        return EXIT_UNAVAILABLE

    # While waiting, Ctrl-C or a kill just ends the wait (closing the connection leaves the queue).
    ev, msg = wait_for_grant(c, req, quiet=a.quiet)
    if ev not in ("granted", "already"):
        c.close()
        return report_failure(ev, msg)
    leases = msg["leases"]
    if not a.quiet:
        err(f"slots: {label} holds {lease_names(leases)}")

    env = dict(os.environ, SLOTS_RUN_TOKEN=token,
               SLOTS_LEASES=",".join(f"{l['resource']}#{l['unit']}" for l in leases))
    try:
        child = subprocess.Popen(command, env=env)
    except OSError as e:
        c.close()
        err(f"slots: can't run {command[0]}: {e}")
        return 127 if isinstance(e, FileNotFoundError) else 126

    # Forward termination to the command. Ctrl-C reaches it directly through the terminal's process
    # group, so the parent only ignores it (forwarding would send it twice).
    def forward(sig, _frame):
        try:
            child.send_signal(sig)
        except ProcessLookupError:
            pass
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    signal.signal(signal.SIGINT, signal.SIG_IGN if sys.stdin.isatty() else forward)

    done = threading.Event()
    threading.Thread(target=hold_connection, args=(c, token, done, a.quiet), daemon=True).start()
    code = child.wait()
    done.set()
    c.close()   # releases the leases
    return 128 - code if code < 0 else code


def hold_connection(c: Client, token: str, done: threading.Event, quiet: bool):
    """Keep the connection that holds a run's leases; if the daemon restarts, take them back."""
    while not done.is_set():
        try:
            msg = c.recv()
        except (TimeoutError, OSError):
            msg = None
        if done.is_set():
            return
        if msg is not None:
            continue
        if not reconnect(c) or done.is_set():
            if not done.is_set() and not quiet:
                err("slots: lost the daemon; the command keeps running, but its slot is no longer held")
            return
        try:
            c.send({"op": "reclaim", "token": token})
            reply = c.recv(5)
        except (TimeoutError, OSError):
            reply = None
        if (reply or {}).get("ev") != "reclaimed" and not quiet:
            err("slots: the daemon restarted and this run's slot was given away; the command keeps running")
            return


def report_failure(ev: str, msg: dict) -> int:
    if ev == "busy":
        err(describe_busy(msg))
        return EXIT_BUSY
    if ev == "timeout":
        err("timed out waiting: " + ("; ".join(fmt.holder(h) for h in msg.get("holders", [])) or "queue ahead"))
        return EXIT_TIMEOUT
    if ev == "dequeued":
        err("removed from the queue by an admin action")
        return EXIT_BUSY
    err(f"slots: {msg.get('message', ev)}")
    return EXIT_USAGE if ev == "error" else EXIT_UNAVAILABLE


def cmd_acquire(ctx: Ctx) -> int:
    a = ctx.args
    owner = ctx.owner(a.label, a.pid)
    if not owner["lane"]:
        err("slots: not inside a lane folder; pass --lane <ticket> (or set SLOTS_LANE)")
        return EXIT_USAGE
    token = secrets.token_hex(8)
    req = {"op": "acquire", "resources": a.resources, "owner": owner, "token": token,
           "wait": a.wait, "timeout": a.timeout, "idempotent": True}
    c = ctx.client()
    try:
        c.connect()
        ev, msg = wait_for_grant(c, req)
        if ev == "already":
            print(f"{owner['lane']} already holds {lease_names(msg['leases'])}")
            return 0
        if ev != "granted":
            return report_failure(ev, msg)
        fresh = [l for l in msg["leases"] if l["token"] == token]
        if fresh:
            c.send({"op": "detach", "token": token, "kind": "pid" if a.pid else "manual"})
            reply = c.recv(10)
            if not reply or reply.get("ev") != "detached":
                err(f"slots: couldn't keep the lease: {(reply or {}).get('message', 'daemon went away')}")
                return EXIT_UNAVAILABLE
        print(f"{owner['lane']} took {lease_names(msg['leases'])}")
        return 0
    except Unavailable as e:
        err(f"slots: {e}")
        return EXIT_UNAVAILABLE
    finally:
        c.close()


def cmd_release(ctx: Ctx) -> int:
    a = ctx.args
    if not a.resources and not a.all:
        err("usage: slots release <resource...> | --all")
        return EXIT_USAGE
    owner = ctx.owner()
    if not owner["lane"]:
        err("slots: not inside a lane folder; pass --lane <ticket> (or set SLOTS_LANE)")
        return EXIT_USAGE
    reply = ctx_call(ctx, {"op": "release", "resources": None if a.all else a.resources, "owner": owner})
    if reply is None:
        return EXIT_UNAVAILABLE
    if reply["ev"] == "refused":
        err(f"slots: refused: {owner['lane']} doesn't hold it; {reply['message'].removeprefix('not yours: ')}")
        return EXIT_BUSY
    if reply["ev"] == "error":
        err(f"slots: {reply['message']}")
        return EXIT_USAGE
    if not reply["leases"]:
        print(f"{owner['lane']} holds nothing to release")
    else:
        print(f"{owner['lane']} released {lease_names(reply['leases'])}")
    return 0


def ctx_call(ctx: Ctx, msg: dict) -> dict | None:
    c = ctx.client()
    try:
        return c.call(msg)
    except Unavailable as e:
        err(f"slots: {e}")
        return None
    finally:
        c.close()


# ---- status / admin ------------------------------------------------------------------------------

def cmd_status(ctx: Ctx) -> int:
    reply = ctx_call(ctx, {"op": "status"})
    if reply is None:
        return EXIT_UNAVAILABLE
    if ctx.args.json:
        json.dump(reply["state"], sys.stdout, indent=2)
        print()
    else:
        print(fmt.status_text(reply["state"]))
    return 0


def confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        err("slots: admin actions need --yes when not run from a terminal")
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def cmd_admin(ctx: Ctx) -> int:
    a = ctx.args
    by = {"lane": "admin", "label": f"admin ({os.environ.get('USER', '?')})", "pid": os.getpid()}
    if a.admin_cmd == "release":
        what = f"{a.resource}" + (f" #{a.unit}" if a.unit else "") + (f" held by {a.holder}" if a.holder else "")
        if not confirm(f"Release {what}?", a.yes):
            return EXIT_BUSY
        state = ctx_call(ctx, {"op": "status"})
        if state is None:
            return EXIT_UNAVAILABLE
        name = ctx.cfg.resolve(a.resource).name
        targets = [h for r in state["state"]["resources"] if r["name"] == name for h in r["holders"]
                   if (a.unit is None or h["unit"] == a.unit) and (a.holder is None or h["owner"].get("lane") == a.holder)]
        if not targets:
            print(f"nothing matches {what}")
            return 0
        released = []
        for h in targets:
            reply = ctx_call(ctx, {"op": "release", "admin": True, "lease_id": h["id"], "resources": [name],
                                   "owner": by, "reason": a.reason})
            released += (reply or {}).get("leases", [])
        print(f"released {lease_names(released) or 'nothing'}")
        return 0
    if a.admin_cmd == "dequeue":
        if not confirm(f"Remove {a.holder} from the {a.resource} queue?", a.yes):
            return EXIT_BUSY
        name = ctx.cfg.resolve(a.resource).name
        reply = ctx_call(ctx, {"op": "dequeue", "resource": name, "lane": a.holder, "owner": by})
        if reply is None:
            return EXIT_UNAVAILABLE
        print(f"removed {reply.get('count', 0)} waiter(s)")
        return 0
    return EXIT_USAGE


# ---- daemon / init -------------------------------------------------------------------------------

def cmd_daemon(ctx: Ctx) -> int:
    a = ctx.args
    if a.daemon_cmd == "run":
        from slots import daemon
        if ctx.config_path is None:
            err("slots: no config found: run `slots init`")
            return EXIT_USAGE
        ctx.cfg  # fail fast on a bad config
        return daemon.run(ctx.paths, ctx.config_path.resolve())
    c = Client(ctx.paths, ctx.config_path, autostart=False)
    try:
        if a.daemon_cmd == "stop":
            c.call({"op": "shutdown"})
            print("daemon stopped")
        else:
            r = c.call({"op": "hello"})
            print(f"daemon pid {r['pid']} version {r['version']} config {r['config']}")
        return 0
    except Unavailable:
        print("daemon not running")
        return 0 if a.daemon_cmd == "stop" else EXIT_UNAVAILABLE
    finally:
        c.close()


def cmd_init(ctx: Ctx) -> int:
    target = Path(ctx.args.path or config.USER_CONFIG).expanduser()
    if target.exists() and not ctx.args.force:
        err(f"slots: {target} already exists (use --force to overwrite)")
        return EXIT_BUSY
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pkg_resources.files("slots").joinpath("example.toml").read_text())
    print(f"wrote {target}")
    return 0


# ---- parser --------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slots", description="Share one machine's heavy work between many sessions.")
    p.add_argument("--config", help="config file (default: $SLOTS_CONFIG, ./.slots.toml, ~/.config/slots/config.toml)")
    p.add_argument("--version", action="version", version=f"slots {__version__}")
    sub = p.add_subparsers(dest="cmd")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **kw: _add(*a, parents=[common], **kw)

    def identity(sp):
        sp.add_argument("--lane", help="the lane (ticket) to act for; default from the working directory")
        sp.add_argument("--session", help="the Claude Code session id; default $SLOTS_SESSION")

    r = sub.add_parser("run", help="run a command while holding resources")
    r.add_argument("resources", nargs="+", metavar="resource")
    r.add_argument("--label")
    r.add_argument("--timeout", type=float, help="give up waiting after this many seconds (exit 124)")
    r.add_argument("--no-wait", action="store_true", help="fail at once (exit 1) instead of waiting")
    r.add_argument("-q", "--quiet", action="store_true")
    identity(r)

    q = sub.add_parser("acquire", help="hold resources across commands until released")
    q.add_argument("resources", nargs="+", metavar="resource")
    q.add_argument("--wait", action="store_true", help="wait in line until granted")
    q.add_argument("--timeout", type=float, help="with --wait: give up after this many seconds (exit 124)")
    q.add_argument("--pid", type=int, help="end the lease when this process exits")
    q.add_argument("--label")
    identity(q)

    rl = sub.add_parser("release", help="release resources you hold")
    rl.add_argument("resources", nargs="*", metavar="resource")
    rl.add_argument("--all", action="store_true")
    identity(rl)

    s = sub.add_parser("status", help="who holds and awaits what")
    s.add_argument("--json", action="store_true")

    ad = sub.add_parser("admin", help="act on someone else's lease (asks first; logged)")
    asub = ad.add_subparsers(dest="admin_cmd", required=True)
    ar = asub.add_parser("release")
    ar.add_argument("resource")
    ar.add_argument("--unit", type=int)
    ar.add_argument("--holder", help="only the lease held by this lane")
    ar.add_argument("--reason")
    ar.add_argument("--yes", action="store_true")
    adq = asub.add_parser("dequeue")
    adq.add_argument("resource")
    adq.add_argument("--holder", required=True, help="the lane to remove from the queue")
    adq.add_argument("--yes", action="store_true")

    d = sub.add_parser("daemon", help="the background broker (started automatically)")
    d.add_argument("daemon_cmd", choices=["run", "stop", "status"])

    i = sub.add_parser("init", help="write a commented example config")
    i.add_argument("--path", help=f"where to write it (default {config.USER_CONFIG})")
    i.add_argument("--force", action="store_true")
    return p


COMMANDS = {"run": cmd_run, "acquire": cmd_acquire, "release": cmd_release, "status": cmd_status,
            "admin": cmd_admin, "daemon": cmd_daemon, "init": cmd_init}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # `run`'s command is everything after the first `--`, untouched by option parsing.
    command: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    args = parser().parse_args(argv)
    args.command = command
    if not args.cmd:
        parser().print_help()
        return EXIT_USAGE
    try:
        ctx = Ctx(args)
        return COMMANDS[args.cmd](ctx)
    except config.ConfigError as e:
        err(f"slots: {e}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
