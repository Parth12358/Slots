"""Watchers: small jobs the daemon runs on an interval or an event, which set status flags.

Each [watcher.NAME] in the config is one of:

  credential   run a command; its exit code says valid or not, and an optional JSON path in its
               output (`expiry`) gives when it runs out. Flag NAME: ok, warn (expires within
               `warn_before`) or bad.
  lane-setup   for each lane whose `port` (a service it leased with `slots port`, or a number) is
               listening: when `artifact` is older than the process listening there (or the last
               run failed), wait `settle`, then run `command` in `cwd`, holding `locks`. Flags
               NAME:<lane>. This is the old sign-in watcher, with nothing hard-coded.
  python       call check(ctx) in the file at `path`; it returns {"state", "text"}, or a list of
               {"name", "state", "text"} for several flags.

Templates in artifact, command, cwd and env: {lane}, {root}, {home}, {port}.
Triggers: `every` (a duration) and/or `on`: "start", "lease:<resource>", "flag:<watcher>".
States: ok, warn, bad. bad and warn go to the dashboard's Needs you.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path

from slots import fmt, machine, proc

OUTPUT_KEPT = 20_000   # bytes of a run's output kept in its log


def template(text: str, **values) -> str:
    out = str(text)
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def dig(data, path: str):
    """`data.expire_time` in a JSON value; None when it isn't there."""
    for part in path.split("."):
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return None
    return data


def parse_time(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        t = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


class Runner:
    def __init__(self, daemon):
        self.d = daemon
        self.specs: dict[str, dict] = {}
        self.next_run: dict[str, float] = {}
        self.running: dict[str, asyncio.Task] = {}
        self.failed: dict[tuple[str, str], bool] = {}     # (watcher, lane) -> last setup failed
        self.last: dict[str, float] = {}
        self.pending: set[str] = set()

    @property
    def broker(self):
        return self.d.broker

    def log_dir(self) -> Path:
        p = self.d.paths.home / "watchers"
        p.mkdir(exist_ok=True)
        return p

    def load(self, specs: dict[str, dict]):
        self.specs = specs
        now = time.time()
        for name in list(self.next_run):
            if name not in specs:
                self.next_run.pop(name)
        for name, spec in specs.items():
            if spec["every"]:
                self.next_run.setdefault(name, now)
            if "start" in spec["on"]:
                self.pending.add(name)

    def trigger(self, event: str):
        for name, spec in self.specs.items():
            if event in spec["on"]:
                self.pending.add(name)

    def run_now(self, name: str) -> bool:
        if name not in self.specs:
            return False
        self.pending.add(name)
        return True

    def tick(self):
        """Start whatever is due (called from the daemon's loop)."""
        now = time.time()
        due = set(self.pending)
        self.pending.clear()
        due |= {n for n, t in self.next_run.items() if t <= now}
        for name in due:
            spec = self.specs.get(name)
            if not spec or (name in self.running and not self.running[name].done()):
                continue
            if spec["every"]:
                self.next_run[name] = now + spec["every"]
            self.running[name] = asyncio.get_running_loop().create_task(self.run(spec))

    def stop(self):
        for t in self.running.values():
            t.cancel()

    def views(self) -> list[dict]:
        now = time.time()
        return [{"name": n, "kind": s["kind"], "every": s["every"], "on": s["on"],
                 "running": n in self.running and not self.running[n].done(),
                 "last_run": self.last.get(n), "next_in": max(0.0, self.next_run[n] - now) if n in self.next_run else None}
                for n, s in sorted(self.specs.items())]

    # ---- running -------------------------------------------------------------------------------

    def flag(self, name: str, state: str, text: str, **extra):
        if self.broker.set_flag(name, state, text, **extra):
            self.trigger(f"flag:{name.split(':')[0]}")
        self.d.persist()

    async def run(self, spec: dict):
        name = spec["name"]
        self.last[name] = time.time()
        try:
            await {"credential": self.credential, "lane-setup": self.lane_setup, "python": self.python}[spec["kind"]](spec)
        except asyncio.CancelledError:
            raise
        except Exception as e:   # a broken watcher reports itself; it never stops the daemon
            self.flag(name, "bad", f"{name}: the watcher failed: {type(e).__name__}: {e}")

    async def shell(self, command: str, *, cwd: str | None, env: dict, timeout: float, log: Path) -> tuple[int, str]:
        p = await asyncio.create_subprocess_shell(
            command, cwd=cwd or str(Path.home()), env={**os.environ, **env}, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
            code = p.returncode
        except asyncio.TimeoutError:
            try:
                os.killpg(p.pid, 15)
            except ProcessLookupError:
                pass
            out, code = b"", 124
        text = out.decode(errors="replace")
        with open(log, "a") as f:
            f.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} exit {code}: {command}\n{text[-OUTPUT_KEPT:]}\n")
        return code, text

    async def credential(self, spec: dict):
        name = spec["name"]
        values = {"home": Path.home(), "root": self.d.cfg.lanes_root or ""}
        code, out = await self.shell(template(spec["command"], **values),
                                     cwd=template(spec["cwd"], **values) if spec.get("cwd") else None,
                                     env={k: template(v, **values) for k, v in spec["env"].items()},
                                     timeout=spec["timeout"], log=self.log_dir() / f"{name}.log")
        if code != 0:
            first = next((l.strip() for l in out.splitlines() if l.strip()), "")
            why = "timed out" if code == 124 else f"exit {code}"
            return self.flag(name, "bad", f"{name}: not valid ({why})" + (f": {first[:120]}" if first else ""))
        if not spec.get("expiry"):
            return self.flag(name, "ok", f"{name}: valid")
        try:
            value = dig(json.loads(out), spec["expiry"])
        except json.JSONDecodeError:
            value = None
        expires = parse_time(value) if value is not None else None
        if expires is None:
            return self.flag(name, "ok", f"{name}: valid (no `{spec['expiry']}` in its output to say until when)")
        left = expires - time.time()
        until = time.strftime("%H:%M", time.localtime(expires))
        if left <= 0:
            return self.flag(name, "bad", f"{name}: expired at {until}", expires=expires)
        state = "warn" if left < spec["warn_before"] else "ok"
        self.flag(name, state, f"{name}: valid until {until} (in {fmt.dur(left)})", expires=expires)

    def lanes(self, spec: dict) -> list[str]:
        if spec.get("lanes"):
            return [str(l) for l in spec["lanes"]]
        root = self.d.cfg.lanes_root
        found = set()
        if root and root.is_dir():
            for p in root.iterdir():
                if p.is_dir() and self.d.cfg.lane_of(p):
                    found.add(self.d.cfg.lane_of(p))
        return sorted(found | {p["lane"] for p in self.broker.ports.values()})

    def port_of(self, spec: dict, lane: str) -> int | None:
        port = str(spec["port"])
        if port.isdigit():
            return int(port)
        lease = self.broker.ports.get(f"{lane}/{port}")
        return lease["port"] if lease else None

    async def lane_setup(self, spec: dict):
        name = spec["name"]
        snap = await asyncio.to_thread(machine.collect)
        listeners = {p["port"]: p["pid"] for p in snap["ports"]}
        for lane in self.lanes(spec):
            port = self.port_of(spec, lane)
            pid = listeners.get(port) if port else None
            if not pid:
                continue
            values = {"lane": lane, "port": port, "home": Path.home(), "root": self.d.cfg.lanes_root or ""}
            started = await asyncio.to_thread(proc.start_epoch, pid)
            artifact = Path(template(spec["artifact"], **values)).expanduser()
            try:
                made = artifact.stat().st_mtime
            except OSError:
                made = 0.0
            key = (name, lane)
            if started is not None and made > started and not self.failed.get(key):
                self.flag(f"{name}:{lane}", "ok", f"{name}: {lane} is up to date")
                continue
            await asyncio.sleep(spec["settle"])     # let the stack settle
            command = template(spec["command"], **values)
            if spec["locks"]:
                # Through slots itself, so it waits its turn like any other run.
                command = f"{self.d.slots_cmd} run {' '.join(map(shlex.quote, spec['locks']))} -q " \
                          f"--label {shlex.quote(f'{lane} {name}')} -- sh -c {shlex.quote(command)}"
            code, out = await self.shell(command, cwd=str(Path(template(spec.get("cwd", "{home}"), **values)).expanduser()),
                                         env={k: template(v, **values) for k, v in spec["env"].items()},
                                         timeout=spec["timeout"], log=self.log_dir() / f"{name}-{lane}.log")
            ok = code == 0 and (not spec.get("success") or spec["success"] in out)
            self.failed[key] = not ok
            if ok:
                self.flag(f"{name}:{lane}", "ok", f"{name}: ran it for {lane}", ran=time.time())
            else:
                why = "timed out" if code == 124 else f"exit {code}" if code else f"no {spec['success']!r} in its output"
                self.flag(f"{name}:{lane}", "bad", f"{name}: failed for {lane} ({why}); will retry", ran=time.time())

    async def python(self, spec: dict):
        name = spec["name"]
        path = Path(template(spec["path"], home=Path.home(), root=self.d.cfg.lanes_root or "")).expanduser()
        mod_spec = importlib.util.spec_from_file_location(f"slots_watcher_{name}", path)
        if mod_spec is None or mod_spec.loader is None:
            return self.flag(name, "bad", f"{name}: can't load {path}")
        module = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(module)
        ctx = {"state": self.broker.snapshot(), "config": self.d.cfg.raw, "lanes": self.lanes(spec), "spec": spec}
        result = await asyncio.wait_for(asyncio.to_thread(module.check, ctx), spec["timeout"])
        for r in result if isinstance(result, list) else [result]:
            state = r.get("state", "ok")
            if state not in ("ok", "warn", "bad"):
                state = "bad"
            flag = f"{name}:{r['name']}" if r.get("name") else name
            self.flag(flag, state, str(r.get("text", "")))
