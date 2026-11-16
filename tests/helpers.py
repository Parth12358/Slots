"""A throwaway slots world per test: its own state dir, config, lanes root and daemon."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

CONFIG = """
[lanes]
root = "{root}"
pattern = '^(?P<ticket>\\d+)$'

[pool.agent]
size = 2
hold = "session"

[pool.test]
size = 2

[lock.e2e]
[lock.next-mock-core]

[family.next-employee]
name = "next-employee-{{key}}"
key = "sha1-12"
"""


class World(unittest.TestCase):
    extra_config = ""

    def setUp(self):
        # Short paths: a Unix socket path must stay under ~100 bytes (macOS temp dirs are long).
        self.dir = Path(tempfile.mkdtemp(prefix="slots-", dir="/tmp"))
        self.home = self.dir / "s"
        self.root = self.dir / "lanes"
        self.root.mkdir()
        self.config = self.dir / "slots.toml"
        self.config.write_text(CONFIG.format(root=self.root) + textwrap.dedent(self.extra_config))
        self.env = {**os.environ, "SLOTS_HOME": str(self.home), "SLOTS_CONFIG": str(self.config),
                    "PYTHONPATH": str(SRC)}
        for k in ("SLOTS_LANE", "SLOTS_SESSION", "SLOTS_DEVICE"):
            self.env.pop(k, None)
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self.procs:
            # Each client has its own process group, so this also ends the commands it started.
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            p.wait()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()
        self.slots("daemon", "stop")
        pid = self.daemon_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---- running the CLI -------------------------------------------------------------------------

    def lane(self, ticket: str) -> Path:
        p = self.root / ticket
        p.mkdir(exist_ok=True)
        return p

    def slots(self, *args, lane: str | None = None, env: dict | None = None, timeout: float = 30,
              check: int | None = None) -> subprocess.CompletedProcess:
        cwd = self.lane(lane) if lane else self.dir
        r = subprocess.run([sys.executable, "-m", "slots", *args], cwd=cwd, env={**self.env, **(env or {})},
                           capture_output=True, text=True, timeout=timeout)
        if check is not None:
            self.assertEqual(r.returncode, check, f"slots {' '.join(args)}\n{r.stdout}\n{r.stderr}")
        return r

    def spawn(self, *args, lane: str | None = None, env: dict | None = None) -> subprocess.Popen:
        cwd = self.lane(lane) if lane else self.dir
        p = subprocess.Popen([sys.executable, "-m", "slots", *args], cwd=cwd, env={**self.env, **(env or {})},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def state(self) -> dict:
        r = self.slots("status", "--json", check=0)
        return json.loads(r.stdout)

    def resource(self, name: str) -> dict:
        for r in self.state()["resources"]:
            if r["name"] == name:
                return r
        return {"name": name, "holders": [], "queue": [], "in_use": 0}

    def daemon_pid(self) -> int | None:
        try:
            return int((self.home / "daemon.pid").read_text().strip())
        except (OSError, ValueError):
            return None

    def until(self, cond, timeout: float = 10, every: float = 0.05, what: str = "condition"):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            v = cond()
            if v:
                return v
            time.sleep(every)
        self.fail(f"timed out waiting for {what}")

    def queue_len(self, name: str) -> int:
        return len(self.resource(name)["queue"])
