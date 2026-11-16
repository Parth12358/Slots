"""The `slots` command."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
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

    @property
    def slots_cmd(self) -> str:
        """How hooks and shims run this slots, with this config."""
        from slots import shims
        return shims.slots_command(self.config_path.resolve() if self.config_path else None)

    def client(self) -> Client:
        return Client(self.paths, self.config_path)

    def owner(self, label: str | None = None, pid: int | None = None) -> dict:
        a = self.args
        lane = getattr(a, "lane", None) or os.environ.get("SLOTS_LANE") or self.cfg.lane_of(os.getcwd())
        session = getattr(a, "session", None) or os.environ.get("SLOTS_SESSION")
        return {"lane": lane, "session": session, "pid": pid, "label": label or lane}


# ---- run / acquire -------------------------------------------------------------------------------

def say_waiting(quiet: bool = False):
    """The default "queued" handler: one line when the wait starts, and again when we're next."""
    said = None

    def on_queued(msg: dict):
        nonlocal said
        line = fmt.waiting_line(msg)
        if not quiet and line != said and (said is None or msg["position"] == 1):
            err(line)
        said = line
    return on_queued


def wait_for_grant(c: Client, req: dict, on_queued=None) -> tuple[str, dict]:
    """Send an acquire and follow it to the end, reconnecting if the daemon restarts.
    Returns ("granted"|"already"|"busy"|"timeout"|"error"|"dequeued", last message)."""
    on_queued = on_queued or say_waiting()
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
                    on_queued(msg)
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
    return run_holding(ctx, a.resources, label, command, wait=not a.no_wait, timeout=a.timeout,
                       quiet=a.quiet, on_queued=say_waiting(a.quiet))


def run_holding(ctx: Ctx, resources: list[str], label: str, command: list[str], *, wait: bool = True,
                timeout: float | None = None, quiet: bool = False, on_queued=None, extra_env: dict | None = None,
                on_failure=None) -> int:
    """Wait for `resources`, run `command` holding them, and return its exit code."""
    token = secrets.token_hex(8)
    req = {"op": "acquire", "resources": resources, "owner": ctx.owner(label, os.getpid()),
           "token": token, "wait": wait, "timeout": timeout}
    c = ctx.client()
    try:
        c.connect()
    except Unavailable as e:
        return (on_failure or report_failure)("unavailable", {"message": str(e)})

    # While waiting, Ctrl-C or a kill just ends the wait (closing the connection leaves the queue).
    ev, msg = wait_for_grant(c, req, on_queued)
    if ev not in ("granted", "already"):
        c.close()
        return (on_failure or report_failure)(ev, msg)
    leases = msg["leases"]
    if not quiet:
        err(f"slots: {label} holds {lease_names(leases)}")

    env = dict(os.environ, SLOTS_RUN_TOKEN=token,
               SLOTS_LEASES=",".join(f"{l['resource']}#{l['unit']}" for l in leases), **(extra_env or {}))
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
    threading.Thread(target=hold_connection, args=(c, token, done, quiet), daemon=True).start()
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


def hold(ctx: Ctx, resources: list[str], owner: dict, *, wait: bool, timeout: float | None = None,
         pid: int | None = None, on_queued=None) -> tuple[str, dict]:
    """Acquire leases that outlive this process (a pid lease with `pid`, else manual).
    Returns wait_for_grant's (event, message); the message's leases include ones already held."""
    token = secrets.token_hex(8)
    req = {"op": "acquire", "resources": resources, "owner": owner, "token": token,
           "wait": wait, "timeout": timeout, "idempotent": True}
    c = ctx.client()
    try:
        c.connect()
        ev, msg = wait_for_grant(c, req, on_queued)
        if ev == "granted" and any(l["token"] == token for l in msg["leases"]):
            kind = "pid" if pid else "session" if owner.get("session") else "manual"
            c.send({"op": "detach", "token": token, "kind": kind})
            reply = c.recv(10)
            if not reply or reply.get("ev") != "detached":
                return "error", {"message": f"couldn't keep the lease: {(reply or {}).get('message', 'daemon went away')}"}
        return ev, msg
    except Unavailable as e:
        return "unavailable", {"message": str(e)}
    finally:
        c.close()


def cmd_acquire(ctx: Ctx) -> int:
    a = ctx.args
    owner = ctx.owner(a.label, a.pid)
    if not owner["lane"]:
        err("slots: not inside a lane folder; pass --lane <ticket> (or set SLOTS_LANE)")
        return EXIT_USAGE
    ev, msg = hold(ctx, a.resources, owner, wait=a.wait, timeout=a.timeout, pid=a.pid)
    if ev == "already":
        print(f"{owner['lane']} already holds {lease_names(msg['leases'])}")
        return 0
    if ev != "granted":
        return report_failure(ev, msg)
    print(f"{owner['lane']} took {lease_names(msg['leases'])}")
    return 0


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
    state = reply["state"]
    if ctx.args.json and not ctx.args.full:
        json.dump(state, sys.stdout, indent=2)
        print()
        return 0
    model = dashboard_model(ctx, state)
    if ctx.args.json:
        json.dump({**state, "needs": [{k: n[k] for k in ("level", "text")} for n in model["needs"]],
                   "lanes": [{k: ln[k] for k in ("name", "pids", "cpu", "rss", "ports")} |
                             {"by_folder": sorted(ln["fallback"]),
                              "leases": [h["id"] for _, h in ln["leases"]]} for ln in model["lanes"]],
                   "machine": {k: v for k, v in model["machine"].items() if k not in ("procs", "cwds")}},
                  sys.stdout, indent=2, default=list)
        print()
    else:
        print(dashboard_text(model))
    return 0


def dashboard_model(ctx: Ctx, state: dict) -> dict:
    from slots import dashboard, machine
    mach = machine.collect(str(ctx.cfg.lanes_root) if ctx.cfg.lanes_root else None)
    return dashboard.build(state, mach, ctx.cfg, config_path=str(ctx.config_path) if ctx.config_path else None)


def dashboard_text(model: dict, actions: bool = False) -> str:
    from slots import dashboard
    return dashboard.text(model, actions=actions)


def cmd_dash(ctx: Ctx) -> int:
    a = ctx.args
    if a.once:
        from slots import tui
        print(dashboard_text(tui.collect(ctx), actions=True))
        return 0
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        err("slots: the dashboard needs a terminal (use `slots dash --once` or `slots status`)")
        return EXIT_USAGE
    from slots import tui
    glyphs = a.glyphs or os.environ.get("SLOTS_GLYPHS", "acs")
    bg = a.bg or os.environ.get("SLOTS_BG", "solid")
    return tui.run(ctx, glyphs, bg)


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


def cmd_shims(ctx: Ctx) -> int:
    from slots import shims
    a = ctx.args
    if ctx.config_path is None:
        err("slots: no config found: run `slots init` first, so the shims know which config to use")
        return EXIT_USAGE
    ctx.cfg  # the shims map onto [compat]; fail now on a bad config
    try:
        report = shims.install(Path(a.dir).expanduser(), ctx.config_path,
                               only=a.only.split(",") if a.only else None, force=a.force)
    except ValueError as e:
        err(f"slots: {e}")
        return EXIT_USAGE
    print("\n".join(report))
    return 0


def cmd_migrate(ctx: Ctx) -> int:
    from slots import migrate
    a = ctx.args
    root = Path(a.from_root).expanduser()
    if not root.is_dir():
        err(f"slots: {root} is not a folder")
        return EXIT_USAGE
    items = migrate.plan(root, ctx.cfg, Path(a.locks).expanduser() if a.locks else None)
    print(migrate.render(items))
    leases = [i.lease for i in items if i.action == "import"]
    if not a.apply:
        print(f"\ndry run: nothing changed. {len(leases)} lease(s) would be imported; run again with --apply.")
        return 0
    if not leases:
        print("\nnothing to import.")
        return 0
    by = {"lane": "admin", "label": f"migrate ({os.environ.get('USER', '?')})"}
    reply = ctx_call(ctx, {"op": "import", "leases": leases, "owner": by})
    if reply is None:
        return EXIT_UNAVAILABLE
    print()
    for r in reply["result"]:
        if "error" in r:
            print(f"failed   {r['resource']}: {r['error']}")
        elif "skipped" in r:
            print(f"skipped  {r['resource']}: {r['skipped']}")
        else:
            print(f"imported {lease_names([r])} for {fmt.who(r['owner'])} ({r['kind']} lease)")
    print(f"old files under {root} were left as they are.")
    return 0


def cmd_help(ctx: Ctx) -> int:
    from slots import manual
    code, text = manual.help_text(ctx.args.topic)
    (print if code == 0 else err)(text)
    return code


def cmd_man(ctx: Ctx) -> int:
    from slots import manual
    if not ctx.args.install:
        sys.stdout.write(manual.man_page())
        return 0
    target = manual.install_man(Path(ctx.args.dir).expanduser() if ctx.args.dir else None)
    print(f"wrote {target}; read it with: man slots")
    found = subprocess.run(["man", "-w", "slots"], capture_output=True, text=True) if shutil.which("man") else None
    if found is None or found.returncode != 0:
        print(f"(if man can't find it, add {target.parent.parent} to MANPATH)")
    return 0


def cmd_port(ctx: Ctx) -> int:
    from slots import ports
    a = ctx.args
    msg = {"op": "port", "lane": a.lane_name, "service": a.service, "want": a.want, "release": a.release}
    if a.want is not None and a.service is None:
        err("slots: --want needs a service")
        return EXIT_USAGE
    reply = ctx_call(ctx, msg)
    if reply is None:
        return EXIT_UNAVAILABLE
    ev = reply["ev"]
    if ev in ("error", "refused"):
        err(f"slots: {'refused: ' if ev == 'refused' else ''}{reply['message']}")
        return EXIT_BUSY if ev == "refused" else EXIT_USAGE
    if ev == "port-released":
        names = ", ".join(f"{p['service']} :{p['port']}" for p in reply["ports"])
        print(f"{a.lane_name} released {names}" if names else f"{a.lane_name} holds no ports")
        return 0
    if ev == "ports":
        if not reply["ports"]:
            if not a.env:
                print(f"{a.lane_name} holds no ports")
            return 0
        for p in sorted(reply["ports"], key=lambda p: p["service"]):
            print(f"{ports.env_name(ctx.cfg.ports['env'], p['service'])}={p['port']}" if a.env
                  else f"{p['service']:<16} {p['port']}")
        return 0
    port = reply["port"]["port"]
    print(f"{reply['env']}={port}" if a.env else port)
    if reply.get("in_use"):
        err(f"slots: note: something is already listening on {port}")
    return 0


def cmd_watchers(ctx: Ctx) -> int:
    a = ctx.args
    if a.run:
        reply = ctx_call(ctx, {"op": "watch-run", "name": a.run})
        if reply is None:
            return EXIT_UNAVAILABLE
        if reply["ev"] == "error":
            err(f"slots: {reply['message']}")
            return EXIT_USAGE
        print(f"started {a.run}; its result shows in `slots watchers` and the dashboard")
        return 0
    reply = ctx_call(ctx, {"op": "watchers"})
    if reply is None:
        return EXIT_UNAVAILABLE
    if a.json:
        json.dump(reply, sys.stdout, indent=2)
        print()
        return 0
    if not reply["watchers"]:
        print("no watchers configured (see slots help config, [watcher.NAME])")
    now = time.time()
    for w in reply["watchers"]:
        when = "running now" if w["running"] else (f"next in {fmt.dur(w['next_in'])}" if w["next_in"] is not None
                                                   else "on " + ", ".join(w["on"]))
        last = f", last ran {fmt.dur(now - w['last_run'])} ago" if w["last_run"] else ""
        print(f"{w['name']}  ({w['kind']}; {when}{last})")
        for f in reply["flags"]:
            if f["name"] == w["name"] or f["name"].startswith(w["name"] + ":"):
                print(f"  {f['state']:<4}  {f['text']}  (since {fmt.clock(f['since'])})")
    return 0


def cmd_hook(ctx: Ctx) -> int:
    from slots import claude
    return claude.run_hook(ctx, ctx.args.event, sys.stdin.read())


def cmd_claude(ctx: Ctx) -> int:
    from slots import claude
    a = ctx.args
    scope = "project" if a.project else "user"
    if a.claude_cmd == "skill":
        text = claude.skill(ctx.cfg)
        if a.print:
            sys.stdout.write(text)
            return 0
        target = Path(a.output).expanduser() if a.output else claude.skill_path(scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        old = target.read_text() if target.exists() else None
        target.write_text(text)
        print(f"{'unchanged' if old == text else 'wrote'} {target}")
        return 0
    if ctx.config_path is None:
        err("slots: no config found: run `slots init` first, so the hooks know which config to use")
        return EXIT_USAGE
    claude.settings(ctx.cfg)   # fail now on a bad [claude] table
    path = claude.settings_path(scope)
    current = claude.load_settings(path)
    new = claude.merged(current, ctx.slots_cmd if a.claude_cmd == "install" else None)
    diff = claude.apply(path, new, ctx.paths.home / "backups", a.dry_run)
    print(diff, end="" if diff.endswith("\n") else "\n")
    if a.dry_run:
        print("(dry run: nothing was written)")
    elif diff != "no change":
        print(f"{'installed hooks in' if a.claude_cmd == 'install' else 'removed slots hooks from'} {path}"
              + (f"; the old file is in {ctx.paths.home / 'backups'}" if current else ""))
        if a.claude_cmd == "install":
            print("New sessions pick them up; running sessions after a restart. Also: slots claude skill")
    return 0


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

    s = sub.add_parser("status", help="who holds and awaits what, lanes, ports, machine (one snapshot)")
    s.add_argument("--json", action="store_true", help="the broker's state as JSON (fast)")
    s.add_argument("--full", action="store_true", help="with --json: add lanes, needs-you and machine")

    ds = sub.add_parser("dash", help="the live dashboard (also: plain `slots` in a terminal)")
    ds.add_argument("--once", action="store_true", help="print one frame as text, without a terminal")
    g = ds.add_mutually_exclusive_group()
    g.add_argument("--acs", dest="glyphs", action="store_const", const="acs",
                   help="safe glyphs (default): line drawing and coloured spaces")
    g.add_argument("--unicode", dest="glyphs", action="store_const", const="unicode",
                   help="rounded corners and block glyphs, when the terminal measures them right")
    b = ds.add_mutually_exclusive_group()
    b.add_argument("--solid", dest="bg", action="store_const", const="solid", help="solid dark background (default)")
    b.add_argument("--transparent", dest="bg", action="store_const", const="transparent",
                   help="keep the terminal's own background")

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

    sh = sub.add_parser("shims", help="install agent-slot.sh, slot.sh and lib-slot.sh shims")
    sh.add_argument("shims_cmd", choices=["install"])
    sh.add_argument("--dir", required=True, help="where to write them")
    sh.add_argument("--only", help="comma-separated: agent-slot.sh,slot.sh,lib-slot.sh")
    sh.add_argument("--force", action="store_true", help="replace existing files (kept as <name>.orig-<time>)")

    m = sub.add_parser("migrate", help="adopt holders from the old scripts (dry run unless --apply)")
    m.add_argument("--from", dest="from_root", required=True, metavar="OLD_ROOT")
    m.add_argument("--locks", help="the old lock folder (default OLD_ROOT/ta-e2e/.locks)")
    m.add_argument("--apply", action="store_true")

    pt = sub.add_parser("port", help="a stable port for a lane's service, never another lane's")
    pt.add_argument("lane_name", metavar="lane")
    pt.add_argument("service", nargs="?", help="e.g. ui, api; without it: the lane's ports")
    pt.add_argument("--want", type=int, help="this port, unless another lane holds it")
    pt.add_argument("--env", action="store_true", help="print NAME=PORT lines (a .env fragment)")
    pt.add_argument("--release", action="store_true", help="give the port (or all the lane's ports) back")

    wt = sub.add_parser("watchers", help="the configured watchers and what they found")
    wt.add_argument("--run", metavar="NAME", help="run this watcher now")
    wt.add_argument("--json", action="store_true")

    hk = sub.add_parser("hook", help="run by Claude Code's hooks (reads the event's JSON on stdin)")
    hk.add_argument("event", choices=["session-start", "user-prompt", "pre-tool-use", "stop", "notification",
                                      "session-end"])

    cl = sub.add_parser("claude", help="Claude Code: install hooks, write the skill")
    csub = cl.add_subparsers(dest="claude_cmd", required=True)
    for name, text in (("install", "add the slots hooks to Claude Code's settings (shows the diff)"),
                       ("uninstall", "remove the slots hooks (and only those)")):
        c = csub.add_parser(name, help=text)
        where = c.add_mutually_exclusive_group()
        where.add_argument("--user", action="store_true", help="~/.claude/settings.json (default)")
        where.add_argument("--project", action="store_true", help="./.claude/settings.local.json")
        c.add_argument("--dry-run", action="store_true", help="show the diff, change nothing")
    ck = csub.add_parser("skill", help="write the local-slots skill, generated from this config")
    where = ck.add_mutually_exclusive_group()
    where.add_argument("--user", action="store_true", help="~/.claude/skills/local-slots/SKILL.md (default)")
    where.add_argument("--project", action="store_true", help="./.claude/skills/local-slots/SKILL.md")
    where.add_argument("-o", "--output", help="write it here instead")
    where.add_argument("--print", action="store_true", help="print it instead")

    h = sub.add_parser("help", help="the manual: topics, or one topic or command")
    h.add_argument("topic", nargs="?")

    mn = sub.add_parser("man", help="the whole manual as a man page")
    mn.add_argument("--install", action="store_true", help="write it to ~/.local/share/man/man1/slots.1")
    mn.add_argument("--dir", help="the man folder to install under (default ~/.local/share/man)")

    i = sub.add_parser("init", help="write a commented example config")
    i.add_argument("--path", help=f"where to write it (default {config.USER_CONFIG})")
    i.add_argument("--force", action="store_true")
    return p


COMMANDS = {"dash": cmd_dash, "run": cmd_run, "acquire": cmd_acquire, "release": cmd_release, "status": cmd_status,
            "admin": cmd_admin, "daemon": cmd_daemon, "init": cmd_init, "shims": cmd_shims,
            "migrate": cmd_migrate, "help": cmd_help, "man": cmd_man,
            "hook": cmd_hook, "claude": cmd_claude, "port": cmd_port,
            "watchers": cmd_watchers}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # `run`'s command is everything after the first `--`, untouched by option parsing.
    command: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    # `slots [--config C] compat <name> ...` takes the old scripts' arguments verbatim.
    head = argv[2:] if argv[:1] == ["--config"] else argv
    if head[:1] == ["compat"]:
        from slots import compat
        args = argparse.Namespace(config=argv[1] if argv[:1] == ["--config"] else None)
        try:
            return compat.main(Ctx(args), head[1:], command)
        except config.ConfigError as e:
            err(f"slots: {e}")
            return EXIT_USAGE
        except KeyboardInterrupt:
            return 130
    args = parser().parse_args(argv)
    args.command = command
    if not args.cmd:
        if sys.stdin.isatty() and sys.stdout.isatty():
            args = parser().parse_args([*argv, "dash"])
            args.command = command
        else:
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
