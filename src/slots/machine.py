"""What the machine is doing: processes, listening ports, memory, swap, load, containers.

Read-only, and the same shape on macOS and Linux. The commands for one refresh are
started together, so a loaded machine doesn't add their latencies up.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time

LINUX = sys.platform.startswith("linux")


def _spawn(cmd: list[str]):
    try:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None


def _finish(p, timeout: float = 3) -> str:
    if p is None:
        return ""
    try:
        return p.communicate(timeout=timeout)[0] or ""
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return ""


def _run(cmd: list[str]) -> str:
    return _finish(_spawn(cmd))


def etime_secs(s: str) -> int:
    """ps etime '[[dd-]hh:]mm:ss' -> seconds."""
    days = 0
    if "-" in s:
        dd, s = s.split("-", 1)
        days = int(dd) if dd.isdigit() else 0
    secs = 0
    for part in s.split(":"):
        secs = secs * 60 + (int(part) if part.isdigit() else 0)
    return days * 86400 + secs


def parse_ps(out: str) -> dict[int, dict]:
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid, ppid, cpu, rss = int(parts[0]), int(parts[1]), float(parts[2]), int(parts[3])
        except ValueError:
            continue
        procs[pid] = {"pid": pid, "ppid": ppid, "cpu": cpu, "rss": rss, "etime": parts[4],
                      "secs": etime_secs(parts[4]), "cmd": parts[5]}
    return procs


def children_of(procs: dict[int, dict]) -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for p in procs.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])
    return kids


def tree(pid: int, procs: dict, kids: dict) -> list[int]:
    out, stack, seen = [], [pid], set()
    while stack:
        p = stack.pop()
        if p in procs and p not in seen:
            seen.add(p)
            out.append(p)
            stack.extend(kids.get(p, []))
    return out


def parse_lsof(out: str) -> list[dict]:
    """`lsof -nP -iTCP -sTCP:LISTEN` -> [{cmd, pid, port}]."""
    ports, seen = [], set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9 or not parts[1].isdigit():
            continue
        addr = parts[-2] if parts[-1] == "(LISTEN)" else parts[-1]
        port = addr.rsplit(":", 1)[-1]
        if port.isdigit() and (parts[1], port) not in seen:
            seen.add((parts[1], port))
            ports.append({"cmd": parts[0], "pid": int(parts[1]), "port": int(port)})
    return ports


def parse_ss(out: str) -> list[dict]:
    """`ss -ltnpH` (Linux without lsof) -> [{cmd, pid, port}]."""
    ports, seen = [], set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        port = parts[3].rsplit(":", 1)[-1]
        for cmd, pid in re.findall(r'\("([^"]+)",pid=(\d+)', line):
            if port.isdigit() and (pid, port) not in seen:
                seen.add((pid, port))
                ports.append({"cmd": cmd, "pid": int(pid), "port": int(port)})
    return ports


_MEMSIZE: list[int] = []


def memory_macos(vm: str, swap: str) -> dict:
    if not _MEMSIZE:
        total = _run(["sysctl", "-n", "hw.memsize"]).strip()
        if total.isdigit():
            _MEMSIZE.append(int(total))
    out = {}
    pg = re.search(r"page size of (\d+) bytes", vm)
    if _MEMSIZE and pg:
        size = int(pg.group(1))

        def pages(k):
            m = re.search(rf"{k}:\s+(\d+)", vm)
            return int(m.group(1)) if m else 0
        out["mem_free"] = (pages("Pages free") + pages("Pages inactive") + pages("Pages speculative")) * size / 2**30
        out["mem_total"] = _MEMSIZE[0] / 2**30
    st, su = re.search(r"total = ([\d.]+)M", swap), re.search(r"used = ([\d.]+)M", swap)
    if st and su:
        out["swap_total"], out["swap_used"] = float(st.group(1)) / 1024, float(su.group(1)) / 1024
    return out


def memory_linux(meminfo: str) -> dict:
    kb = {k: int(v) for k, v in re.findall(r"^(\w+):\s+(\d+) kB", meminfo, re.M)}
    out = {}
    if "MemTotal" in kb:
        out["mem_total"] = kb["MemTotal"] / 2**20
        out["mem_free"] = kb.get("MemAvailable", kb.get("MemFree", 0)) / 2**20
    if "SwapTotal" in kb:
        out["swap_total"] = kb["SwapTotal"] / 2**20
        out["swap_used"] = (kb["SwapTotal"] - kb.get("SwapFree", 0)) / 2**20
    return out


def lane_cwds(procs: dict, root: str) -> dict[int, str]:
    """Linux only: pid -> the folder directly under root that its working directory is in."""
    out = {}
    if not LINUX or not root:
        return out
    prefix = root.rstrip("/") + "/"
    for pid in procs:
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if cwd.startswith(prefix):
            out[pid] = cwd[len(prefix):].split("/", 1)[0]
    return out


def collect(root: str | None = None) -> dict:
    """One snapshot. `root` (the lanes root) enables working-directory attribution on Linux."""
    t0 = time.monotonic()
    use_lsof = shutil.which("lsof") is not None
    jobs = {"ps": _spawn(["ps", "-axo", "pid=,ppid=,pcpu=,rss=,etime=,command="]),
            "ports": _spawn(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]) if use_lsof
            else _spawn(["ss", "-ltnpH"]) if shutil.which("ss") else None,
            "docker": _spawn(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"])
            if shutil.which("docker") else None}
    if not LINUX:
        jobs["vm"], jobs["swap"] = _spawn(["vm_stat"]), _spawn(["sysctl", "-n", "vm.swapusage"])
    procs = parse_ps(_finish(jobs["ps"]))
    ports_out = _finish(jobs["ports"])
    ports = parse_lsof(ports_out) if use_lsof else parse_ss(ports_out)
    ports.sort(key=lambda p: p["port"])
    if LINUX:
        try:
            with open("/proc/meminfo") as f:
                mem = memory_linux(f.read())
        except OSError:
            mem = {}
    else:
        mem = memory_macos(_finish(jobs["vm"]), _finish(jobs["swap"]))
    containers = None
    if jobs["docker"] is not None:
        containers = [l.split("\t") for l in _finish(jobs["docker"]).splitlines() if l.strip()]
    try:
        load = list(os.getloadavg())
    except OSError:
        load = None
    real_root = os.path.realpath(root) if root else None
    return {"procs": procs, "ports": ports, "containers": containers, "load": load, "cores": os.cpu_count() or 1,
            **mem, "cwds": lane_cwds(procs, real_root) if real_root else {}, "took": time.monotonic() - t0,
            "self": os.getpid()}
