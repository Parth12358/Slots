"""Process facts that work the same on macOS and Linux."""
from __future__ import annotations

import os
import subprocess
import sys


LINUX = sys.platform.startswith("linux")


def _linux_stat(pid: int) -> list[str] | None:
    """/proc/<pid>/stat from field 3 (state) on; the command name in field 2 can hold spaces and parens."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read().decode(errors="replace")
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    return fields if len(fields) > 19 else None


def start_time(pid: int) -> str | None:
    """An opaque token for when pid started, so a reused pid isn't mistaken for the original."""
    if LINUX:
        fields = _linux_stat(pid)
        return fields[19] if fields else None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if LINUX:
        fields = _linux_stat(pid)
        return fields is not None and fields[0] not in ("Z", "X")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def alive(pid: int | None, started: str | None) -> bool:
    """pid is running and, when its start time was recorded, is the same process."""
    if not pid or not exists(pid):
        return False
    if started is None:
        return True
    now = start_time(pid)
    return now is None or now == started


def start_epoch(pid: int) -> float | None:
    """When pid started, as a Unix time (for "is this file older than that process?")."""
    if LINUX:
        fields = _linux_stat(pid)
        if not fields:
            return None
        try:
            with open("/proc/stat") as f:
                btime = next(int(l.split()[1]) for l in f if l.startswith("btime "))
        except (OSError, StopIteration, ValueError):
            return None
        return btime + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    import time as _time
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=3,
                             env={**os.environ, "LC_ALL": "C"}).stdout.strip()
        return _time.mktime(_time.strptime(out, "%a %b %d %H:%M:%S %Y")) if out else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None

