"""The broker daemon: one per machine, on a Unix socket, started on demand by the CLI.

Protocol: newline-delimited JSON. Each request is {"op": ..., ...}; each reply or
push is {"ev": ...}. A connection that is waiting for a resource gets "queued"
pushes while its place changes and one "granted" (or "timeout") at the end.
Leases granted on a connection are held by that connection until the client
sends "detach" (turning them into pid or manual leases) or "done"; a
connection that closes, for any reason, releases them.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import socket
import sys
import time
from dataclasses import asdict
from pathlib import Path

from slots import __version__, config, proc
from slots.broker import Broker, Outcome, Owner, Refused
from slots.paths import Paths
from slots.store import Store

TICK = 0.2          # waiter timeouts and reclaim grace
SWEEP_EVERY = 10    # ticks between pid-liveness sweeps and config checks (2 s)


def log(msg: str):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


class Conn:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer

    def send(self, msg: dict):
        if self.writer.is_closing():
            return
        try:
            self.writer.write(json.dumps(msg).encode() + b"\n")
        except (ConnectionError, RuntimeError):
            pass


class Daemon:
    def __init__(self, paths: Paths, config_path: Path):
        self.paths = paths
        self.config_path = config_path
        self.host = socket.gethostname()

    # ---- lifecycle -----------------------------------------------------------------------------

    def run(self) -> int:
        self.paths.ensure()
        lock_fd = os.open(self.paths.pidfile, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another daemon is already running")
            os.close(lock_fd)
            return 0
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, f"{os.getpid()}\n".encode())
        try:
            return asyncio.run(self.main())
        finally:
            os.close(lock_fd)

    async def main(self) -> int:
        self.cfg = config.load(self.config_path)
        self.cfg_mtime = self._mtime()
        self.store = Store(self.paths.db)
        if self.store.recovered_from:
            log(f"state was unreadable; moved it to {self.store.recovered_from} and started empty")
        self.broker = Broker(self.cfg.resources)
        self.broker.load_rows(self.store.load(), self._resolve_quiet)
        self.broker.emit("daemon-start", pid=os.getpid(), leases=len(self.broker.leases),
                         waiters=len(self.broker.waiters))
        self.out(self.broker.sweep())
        self.started = time.time()

        self.stopping = asyncio.Event()
        sock = str(self.paths.socket)
        if os.path.exists(sock):
            os.unlink(sock)   # left by a daemon that was killed; we hold the pidfile lock, so it's not live
        old_umask = os.umask(0o077)
        try:
            server = await asyncio.start_unix_server(self.handle, path=sock)
        finally:
            os.umask(old_umask)
        log(f"slots daemon {__version__} pid {os.getpid()} on {sock} with {self.config_path}")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, self.stopping.set)
        ticker = asyncio.create_task(self.ticker())
        await self.stopping.wait()
        ticker.cancel()
        server.close()
        self.persist()
        os.unlink(sock)
        self.store.close()
        log("stopped")
        return 0

    def _mtime(self) -> float | None:
        try:
            return self.config_path.stat().st_mtime
        except OSError:
            return None

    def _resolve_quiet(self, name: str):
        try:
            return self.cfg.resolve(name)
        except config.ConfigError:
            return None

    async def ticker(self):
        n = 0
        while True:
            await asyncio.sleep(TICK)
            n += 1
            out = self.broker.tick()
            if n % SWEEP_EVERY == 0:
                out.merge(self.broker.sweep())
                out.merge(self.reload_if_changed())
            self.out(out)

    def reload_if_changed(self) -> Outcome:
        mtime = self._mtime()
        if mtime == self.cfg_mtime:
            return Outcome()
        self.cfg_mtime = mtime
        try:
            cfg = config.load(self.config_path)
        except config.ConfigError as e:
            log(f"config reload failed, keeping the old one: {e}")
            return Outcome()
        self.cfg = cfg
        log("config reloaded")
        return self.broker.update_resources(cfg.resources)

    # ---- effects of broker changes -------------------------------------------------------------

    def out(self, out: Outcome):
        """Tell clients what changed, update queue positions, and persist."""
        for g in out.grants:
            if g.waiter.conn:
                g.waiter.conn.send({"ev": "granted", "leases": [self.lrow(l) for l in g.leases]})
        for w in out.timed_out:
            if w.conn:
                w.conn.send({"ev": "timeout", "holders": self.describe(self.broker.blockers(w))})
        for w in self.broker.waiters:
            if w.conn:
                pos = self.broker.position(w)
                if pos != w.position:
                    w.position = pos
                    w.conn.send(self.queued(w))
        if out.grants or out.released or out.timed_out or out.dropped or self.broker.new_events:
            self.persist()

    def persist(self):
        self.store.save(self.broker.rows(), self.broker.take_events())

    def queued(self, w) -> dict:
        return {"ev": "queued", "position": self.broker.position(w), "resources": w.resources,
                "holders": self.describe(self.broker.blockers(w)),
                "in_use": {n: [len(self.broker.held(n)), self.broker.resources[n].size] for n in w.resources}}

    def lrow(self, lease) -> dict:
        res = self.broker.resources.get(lease.resource)
        return {**lease.row(), "pool": bool(res and res.kind == "pool")}

    def describe(self, leases) -> list[dict]:
        return [{**self.lrow(l), "owner": asdict(l.owner)} for l in leases]

    # ---- connections ---------------------------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        conn = Conn(writer)
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                    reply = self.dispatch(conn, msg)
                except (ValueError, KeyError, TypeError) as e:
                    reply = {"ev": "error", "message": f"bad request: {e}"}
                if reply is not None:
                    conn.send(reply)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            # On shutdown, connection-held leases stay in the state file for the next daemon to hand back.
            if not self.stopping.is_set():
                self.out(self.broker.drop_conn(conn))
            writer.close()

    def owner(self, d: dict | None) -> Owner:
        o = Owner.of(d)
        o.host = o.host or self.host
        if o.pid and not o.pid_start:
            o.pid_start = proc.start_time(o.pid)
        return o

    def dispatch(self, conn: Conn, msg: dict) -> dict | None:
        op = msg.get("op")
        b = self.broker

        if op == "hello":
            return {"ev": "hello", "pid": os.getpid(), "version": __version__, "config": str(self.config_path)}

        if op == "status":
            return {"ev": "status", "state": {
                **b.snapshot(),
                "daemon": {"pid": os.getpid(), "version": __version__, "since": self.started,
                           "config": str(self.config_path), "device": self.cfg.device, "host": self.host},
                "lanes_root": str(self.cfg.lanes_root) if self.cfg.lanes_root else None,
            }}

        if op == "acquire":
            try:
                res = [self.cfg.resolve(s) for s in msg["resources"]]
            except config.ConfigError as e:
                return {"ev": "error", "message": str(e)}
            for r in res:
                b.ensure(r)
            try:
                status, w, leases, out = b.request(
                    [r.name for r in res], self.owner(msg.get("owner")), token=msg.get("token"), conn=conn,
                    wait=bool(msg.get("wait")), timeout=msg.get("timeout"), idempotent=bool(msg.get("idempotent")))
            except ValueError as e:
                return {"ev": "error", "message": str(e)}
            # The requester's own grant is the reply; everyone else's goes out as pushes.
            out.grants = [g for g in out.grants if g.waiter is not w]
            if status == "queued":
                w.position = b.position(w)
                conn.send(self.queued(w))
            self.out(out)
            if status in ("granted", "already"):
                self.persist()
                return {"ev": status, "leases": [self.lrow(l) for l in leases]}
            if status == "busy":
                return {"ev": "busy", "resources": [r.name for r in res],
                        "holders": self.describe(l for r in res for l in b.held(r.name)),
                        "queue": [asdict(q.owner) for q in sorted(b.waiters, key=lambda q: q.seq)
                                  if set(q.resources) & {r.name for r in res}]}
            return None

        if op == "detach":
            owner = self.owner(msg["owner"]) if msg.get("owner") else None
            try:
                leases = b.detach(msg["token"], msg.get("kind", "manual"), owner)
            except ValueError as e:
                return {"ev": "error", "message": str(e)}
            self.persist()
            return {"ev": "detached", "leases": [self.lrow(l) for l in leases]}

        if op == "reclaim":
            leases = b.reclaim(msg["token"], conn)
            if leases:
                self.persist()
            return {"ev": "reclaimed" if leases else "lost", "leases": [self.lrow(l) for l in leases]}

        if op == "done":
            out = b.release_token(msg["token"])
            self.out(out)
            return {"ev": "released", "leases": [self.lrow(l) for l in out.released]}

        if op == "release":
            admin = bool(msg.get("admin"))
            names = None
            if msg.get("resources"):
                try:
                    names = [self.cfg.resolve(s).name for s in msg["resources"]]
                except config.ConfigError as e:
                    return {"ev": "error", "message": str(e)}
            if admin and names is None and not msg.get("lease_id"):
                return {"ev": "error", "message": "an admin release must name a resource or lease"}
            owner = self.owner(msg.get("owner"))
            try:
                released, out = b.release(names, owner, admin=admin, unit=msg.get("unit"), lease_id=msg.get("lease_id"))
            except Refused as e:
                return {"ev": "refused", "message": str(e), "holders": self.describe(e.leases)}
            if admin and released:
                self.store.log_admin({"action": "release", "by": asdict(owner), "reason": msg.get("reason"),
                                      "leases": [self.lrow(l) for l in released]})
            self.out(out)
            return {"ev": "released", "leases": [self.lrow(l) for l in released]}

        if op == "dequeue":
            matches = [w for w in b.waiters
                       if (not msg.get("token") or w.token == msg["token"])
                       and (not msg.get("lane") or w.owner.lane == msg["lane"])
                       and (not msg.get("resource") or msg["resource"] in w.resources)]
            if not msg.get("token") and not msg.get("lane"):
                return {"ev": "error", "message": "dequeue needs a lane or a token"}
            out = Outcome()
            for w in matches:
                out.merge(b.dequeue(w, "admin-dequeue"))
                if w.conn:
                    w.conn.send({"ev": "dequeued"})
            if matches:
                self.store.log_admin({"action": "dequeue", "by": msg.get("owner"),
                                      "waiters": [x.row() for x in matches]})
            self.out(out)
            return {"ev": "dequeued", "count": len(matches)}

        if op == "touch":
            n = b.touch(self.owner(msg.get("owner")))
            return {"ev": "touched", "leases": n}

        if op == "shutdown":
            self.stopping.set()
            return {"ev": "bye"}

        return {"ev": "error", "message": f"unknown op {op!r}"}


def run(paths: Paths, config_path: Path) -> int:
    return Daemon(paths, config_path).run()
