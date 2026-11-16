"""Talking to the daemon, starting it first when nothing is listening."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from slots.paths import Paths

START_TIMEOUT = 5.0


class Unavailable(Exception):
    pass


class Client:
    def __init__(self, paths: Paths, config_path: Path | None, *, autostart: bool = True):
        self.paths = paths
        self.config_path = config_path.resolve() if config_path else None
        self.autostart = autostart
        self.sock: socket.socket | None = None
        self.buf = b""

    # ---- connection ----------------------------------------------------------------------------

    def _try_connect(self) -> socket.socket | None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(self.paths.socket))
            return s
        except (FileNotFoundError, ConnectionRefusedError):
            s.close()
            return None

    def start_daemon(self):
        if self.config_path is None:
            raise Unavailable("the daemon isn't running and there is no config to start it with")
        self.paths.ensure()
        env = dict(os.environ, SLOTS_HOME=str(self.paths.home))
        src = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        with open(self.paths.log, "ab") as log:
            subprocess.Popen([sys.executable, "-m", "slots", "--config", str(self.config_path), "daemon", "run"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                             start_new_session=True, cwd="/")

    def connect(self) -> Client:
        self.close()
        s = self._try_connect()
        if s is None and self.autostart:
            self.start_daemon()
            deadline = time.monotonic() + START_TIMEOUT
            while s is None and time.monotonic() < deadline:
                time.sleep(0.05)
                s = self._try_connect()
        if s is None:
            raise Unavailable(f"can't reach the slots daemon at {self.paths.socket} (see {self.paths.log})")
        self.sock, self.buf = s, b""
        return self

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    # ---- messages ------------------------------------------------------------------------------

    def send(self, msg: dict):
        assert self.sock
        self.sock.sendall(json.dumps(msg).encode() + b"\n")

    def recv(self, timeout: float | None = None) -> dict | None:
        """The next message, or None when the daemon closed the connection. Raises TimeoutError."""
        assert self.sock
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise TimeoutError from None
            except (ConnectionError, OSError):
                return None
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, msg: dict, timeout: float = 10.0) -> dict:
        """One request and its reply, on a fresh connection if needed."""
        if self.sock is None:
            self.connect()
        self.send(msg)
        reply = self.recv(timeout)
        if reply is None:
            raise Unavailable("the slots daemon closed the connection")
        return reply


def call(paths: Paths, config_path: Path | None, msg: dict, **kw) -> dict:
    c = Client(paths, config_path, **kw)
    try:
        return c.call(msg)
    finally:
        c.close()
