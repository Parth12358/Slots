"""Where the tool keeps its state: $SLOTS_HOME, default ~/.local/state/slots on macOS and Linux."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# sun_path is 104 bytes on macOS and 108 on Linux.
MAX_SOCKET_PATH = 100


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def default(cls) -> Paths:
        return cls(Path(os.environ.get("SLOTS_HOME") or "~/.local/state/slots").expanduser())

    @property
    def socket(self) -> Path:
        return self.home / "daemon.sock"

    @property
    def pidfile(self) -> Path:
        return self.home / "daemon.pid"

    @property
    def db(self) -> Path:
        return self.home / "state.db"

    @property
    def log(self) -> Path:
        return self.home / "daemon.log"

    def ensure(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if len(str(self.socket)) > MAX_SOCKET_PATH:
            raise OSError(f"socket path too long for a Unix socket: {self.socket} (set SLOTS_HOME to a shorter folder)")
