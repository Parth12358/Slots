"""The dashboard's model: what needs you, lanes and their processes, resources, activity, machine.

`build()` is pure: broker state + a machine snapshot + config in, a model out. The
live view (tui.py), `slots dash --once` and `slots status` all render the same
model. Actions go through the broker's admin API (logged by the daemon), or edit a
pool's size in the config file; none of them touch Docker or protected ports.
"""
from __future__ import annotations

import os
import re
import signal
import time

from slots import config, config_edit, fmt, machine

# lsof truncates COMMAND to 9 chars: com.docke(r), postgres, redis-ser(ver), ...
DEV_PORT_CMD = re.compile(r"^(node|com\.docke|docker|vpnkit|postgres|redis|python|Python|java|bun|deno|vite|tsx|"
                          r"esbuild|nginx|temporal|ruby|go|kubectl|ssh|cloudflar|ngrok)", re.I)
CLAUDE_CMD = re.compile(r"^(\d+\.\d+\.\d+|claude)$")    # Claude Code sessions show up as their version
DOCKER_CMD = re.compile(r"(^|/)(com\.docker|docker|dockerd|containerd|vpnkit|colima|lima|qemu)", re.I)
HOT_CPU = 80.0


def lease_rows(state: dict):
    for r in state["resources"]:
        for h in r["holders"]:
            yield r, h


def build(state: dict, mach: dict, cfg: config.Config, *, now: float | None = None,
          config_path: str | None = None) -> dict:
    now = now or state.get("now") or time.time()
    procs = mach.get("procs", {})
    kids = machine.children_of(procs)
    pid_ports: dict[int, set[int]] = {}
    for p in mach.get("ports", []):
        pid_ports.setdefault(p["pid"], set()).add(p["port"])
    lanes: dict[str, dict] = {}

    def lane(name: str) -> dict:
        return lanes.setdefault(name, {"name": name, "leases": [], "waiting": [], "exact": set(), "fallback": set()})

    # Exact: the process trees of leases' holders (`slots run` clients and pid leases). A lease held
    # for a Claude Code session is tied to the session's own process, whose tree is everything the
    # session runs (its shell, its MCP servers): that's shown on the session, not counted as lane work.
    session_pids = {x["pid"] for x in state.get("sessions", []) if x.get("pid")}
    for r, h in lease_rows(state):
        name = h["owner"].get("lane") or h["owner"].get("label") or "?"
        ln = lane(name)
        ln["leases"].append((r, h))
        pid = h["owner"].get("pid")
        if pid and h["kind"] in ("conn", "pid") and pid in procs and pid not in session_pids:
            ln["exact"].update(machine.tree(pid, procs, kids))
    for r in state["resources"]:
        for w in r["queue"]:
            if r["name"] == w["resources"][0]:   # list a multi-resource waiter once
                lane(w["owner"].get("lane") or w["owner"].get("label") or "?")["waiting"].append(w)

    # Fallback for processes started outside slots: a command line or working folder in a lane.
    root = str(cfg.lanes_root) if cfg.lanes_root else None
    if root:
        prefix = re.compile(re.escape(os.path.realpath(root).rstrip("/")) + r"/([^/\s]+)/|" +
                            re.escape(root.rstrip("/")) + r"/([^/\s]+)/")
        cwds = mach.get("cwds", {})
        claimed = set().union(*(l["exact"] for l in lanes.values())) if lanes else set()
        for p in procs.values():
            if p["pid"] in claimed or p["pid"] == mach.get("self") or not cfg.process_pattern.search(p["cmd"]):
                continue
            m = prefix.search(p["cmd"])
            folder = (m.group(1) or m.group(2)) if m else cwds.get(p["pid"])
            if not folder:
                continue
            lm = cfg.lane_pattern.search(folder)
            if lm:
                lane(lm.groupdict().get("ticket") or lm.group(0))["fallback"].add(p["pid"])

    for ln in lanes.values():
        pids = ln["exact"] | ln["fallback"]
        ln["pids"] = sorted(pids)
        ln["cpu"] = sum(procs[p]["cpu"] for p in pids if p in procs)
        ln["rss"] = sum(procs[p]["rss"] for p in pids if p in procs)
        ln["ports"] = sorted(set().union(*(pid_ports.get(p, set()) for p in pids)) if pids else set())

    sessions = []
    for x in state.get("sessions", []):
        pids = [p for p in machine.tree(x["pid"], procs, kids) if p != x["pid"]] if x.get("pid") in procs else []
        held = [(r, h) for r, h in lease_rows(state) if h["id"] in set(x.get("leases", []))]
        sessions.append({**x, "pids": pids, "held": held, "alive": x.get("pid") in procs,
                         "cpu": sum(procs[p]["cpu"] for p in pids), "rss": sum(procs[p]["rss"] for p in pids)})
        if x.get("lane"):
            lane(x["lane"]).setdefault("sessions", []).append(x["id"])

    # What needs a person, most urgent first.
    warn = cfg.warn
    needs = []
    mem_free, mem_total = mach.get("mem_free"), mach.get("mem_total")
    swap_used, swap_total = mach.get("swap_used"), mach.get("swap_total")
    level = swap_level(mach)
    if level == "hot":
        needs.append({"level": "hot", "text": f"machine is thrashing: {mem_free or 0:.1f} GB free, swap "
                      f"{swap_used or 0:.1f}/{swap_total or 0:.1f} GB", "fix": None})
    daemon = state.get("daemon", {})
    if config_path and daemon.get("config") and os.path.realpath(daemon["config"]) != os.path.realpath(config_path):
        needs.append({"level": "warn", "text": f"the daemon uses {daemon['config']}, not {config_path}: "
                      "run `slots daemon stop` so the next command restarts it", "fix": None})
    for r, h in lease_rows(state):
        if h["kind"] == "conn":
            continue
        name = h["owner"].get("lane") or "?"
        ln = lanes.get(name, {})
        age = now - h["since"]
        quiet = now - max(h["since"], h.get("last_active") or 0)
        what = f"{r['name']} slot {h['unit']}" if r["kind"] == "pool" else r["name"]
        act = {"kind": "lease", "resource": r, "lease": h}
        sess = next((x for x in sessions if x["id"] == h["owner"].get("session")), None)
        if sess and sess["state"] == "busy":
            quiet = 0   # its session is working on a turn right now
        elif sess:
            quiet = now - max(sess["state_since"], h["since"])
        if quiet >= warn["idle_min"] * 60 and not ln.get("pids"):
            nopid = "; no pid ties it to a process" if h["kind"] == "manual" else ""
            who = f"session {sess['id'][:8]} ({name}) is idle and" if sess else f"{name}"
            needs.append({"level": "warn", "text": f"idle? {who} holds {what} with no processes for "
                          f"{fmt.dur(quiet)}{nopid}", "fix": act, "fix_label": "release"})
        elif age >= warn["long_held_h"] * 3600:
            needs.append({"level": "warn", "text": f"long-held: {name} has held {what} for {fmt.dur(age)}",
                          "fix": act, "fix_label": "release"})
    for r in state["resources"]:
        for w in r["queue"]:
            if r["name"] == w["resources"][0] and w["waited"] >= warn["wait_min"] * 60:
                needs.append({"level": "warn", "text": f"{fmt.who(w['owner'])} has waited {fmt.dur(w['waited'])} for "
                              f"{', '.join(w['resources'])}", "fix": {"kind": "waiter", "resource": r, "waiter": w},
                              "fix_label": "move to front"})
        if r["in_use"] > r["size"]:
            needs.append({"level": "note", "text": f"{r['name']} is over its limit ({r['in_use']}/{r['size']}) "
                          "since the limit was lowered; no new grants until it's under", "fix": None})

    dev_ports = [p for p in mach.get("ports", []) if DEV_PORT_CMD.match(p["cmd"])]
    claude_ports = [p for p in mach.get("ports", []) if CLAUDE_CMD.match(p["cmd"])]
    return {
        "now": now, "needs": needs, "lanes": sorted(lanes.values(), key=lambda l: (-l["cpu"], l["name"])),
        "sessions": sessions,
        "resources": state["resources"], "events": state.get("events", [])[-40:], "daemon": daemon,
        "ports": dev_ports, "hidden_ports": len(mach.get("ports", [])) - len(dev_ports),
        "claude_ports": len(claude_ports), "containers": mach.get("containers"), "machine": mach,
        "level": level, "warn": warn, "device": cfg.device,
    }


def swap_level(m: dict) -> str:
    """ok | warn | hot. Hot above 80% swap used, or with under 1.5 GB of memory free (thrashing)."""
    free, sw, tot = m.get("mem_free"), m.get("swap_used"), m.get("swap_total")
    if free is not None and free < 1.5:
        return "hot"
    if not tot:
        return "ok"
    pct = (sw or 0) / tot
    return "hot" if pct > 0.8 else "warn" if pct > 0.5 else "ok"


def mb(kb: float) -> str:
    return f"{kb / 1024:.0f}MB" if kb < 1024 * 1024 else f"{kb / 1024 / 1024:.1f}GB"


# ---- plain text (slots dash --once, slots status) ------------------------------------------------

def event_text(e: dict) -> str:
    who = e.get("label") or e.get("lane") or ""
    kind = e["kind"]
    if kind in ("granted",):
        extra = f" after {fmt.dur(e['waited'])}" if e.get("waited", 0) >= 1 else ""
        return f"{who} took {', '.join(e.get('resources', []))}{extra}"
    if kind in ("released", "admin-release", "holder-died", "lost", "expired"):
        why = {"released": "released", "admin-release": "released by admin:", "holder-died": "holder died:",
               "lost": "not reclaimed after a restart:", "expired": "expired (ttl):"}[kind]
        res = f"{e.get('resource')}#{e.get('unit')}"
        return f"{why} {res} ({who}, held {fmt.dur(e.get('held', 0))})" if kind != "released" else \
            f"{who} released {res} after {fmt.dur(e.get('held', 0))}"
    if kind in ("queued", "gave-up", "timed-out", "left-queue", "admin-dequeue", "promoted"):
        verb = {"queued": "is waiting for", "gave-up": "stopped waiting for", "timed-out": "timed out waiting for",
                "left-queue": "left the queue for", "admin-dequeue": "was dequeued by admin from",
                "promoted": "was moved to the front for"}[kind]
        return f"{who} {verb} {', '.join(e.get('resources', []))}"
    if kind == "session-start":
        return f"session {e.get('session', '')[:8]} started in {who or 'no lane'} ({e.get('source') or 'startup'})"
    if kind == "session-end":
        rel = f"; released {', '.join(e['released'])}" if e.get("released") else ""
        return f"session {e.get('session', '')[:8]} ended ({e.get('why')}){rel}"
    if kind == "session-ended":
        return f"released {e.get('resource')}#{e.get('unit')} as {who}'s session ended"
    if kind == "daemon-start":
        return f"daemon started (pid {e.get('pid')}; {e.get('leases', 0)} leases, {e.get('waiters', 0)} waiting)"
    if kind == "imported":
        return f"imported {e.get('resource')}#{e.get('unit')} for {who}"
    if kind == "config":
        return "config reloaded"
    return f"{kind} {who}".strip()


def text(model: dict, *, actions: bool = False) -> str:
    """The whole view as plain text. With actions, rows that have one are marked '*'."""
    out = []

    def line(s: str, act=None, indent: bool = True):
        out.append(("* " if act else "  ") * actions + ("  " if indent else "") + s)

    now, m = model["now"], model["machine"]
    out.append(summary(model))
    if model["needs"]:
        out.append("NEEDS YOU")
        for n in model["needs"]:
            line(f"[{n['level']}] {n['text']}" + (f"  ({n['fix_label']})" if n.get("fix") else ""), n.get("fix"))
    out.append(f"SESSIONS  {len(model['sessions'])}")
    if not model["sessions"]:
        line("none (install the hooks: slots claude install)")
    for x in model["sessions"]:
        held = ", ".join(f"{r['name']}#{h['unit']}" if r["kind"] == "pool" else r["name"] for r, h in x["held"])
        line(f"{x['id'][:8]}  {x.get('lane') or '-':<10} {x['state']} {fmt.dur(now - x['state_since'])}  "
             f"{held or 'holds nothing'}  {len(x['pids'])} procs {mb(x['rss'])}", {"kind": "session"})
    out.append(f"LANES  {len(model['lanes'])}")
    if not model["lanes"]:
        line("none")
    for ln in model["lanes"]:
        held = ", ".join(f"{r['name']}#{h['unit']}" if r["kind"] == "pool" else r["name"] for r, h in ln["leases"])
        procs = f"{len(ln['pids'])} procs {ln['cpu']:.0f}% {mb(ln['rss'])}" if ln["pids"] else "no processes"
        if ln["fallback"]:
            procs += f" ({len(ln['fallback'])} by folder)"
        ports = ("  " + " ".join(f":{p}" for p in ln["ports"][:6])) if ln["ports"] else ""
        wait = ("  waiting for " + ", ".join(",".join(w["resources"]) for w in ln["waiting"])) if ln["waiting"] else ""
        line(f"{ln['name']:<10} {held or '-':<24} {procs}{ports}{wait}", {"kind": "lane"} if ln["pids"] else None)
    for r in model["resources"]:
        head = f"{r['name'].upper()}  {r['kind']}  "
        head += f"{r['in_use']}/{r['size']} in use" if r["kind"] == "pool" else ("held" if r["in_use"] else "free")
        out.append(head + (f", {len(r['queue'])} waiting" if r["queue"] else ""))
        for h in r["holders"]:
            tag = f"#{h['unit']} " if r["kind"] == "pool" else ""
            kind = {"conn": "run", "pid": f"pid {h['owner'].get('pid')}", "manual": "no pid"}[h["kind"]]
            line(f"{tag}{fmt.who(h['owner'])}  since {fmt.clock(h['since'])} ({fmt.dur(now - h['since'])})  [{kind}]",
                 {"kind": "lease"})
        for i, w in enumerate(r["queue"], 1):
            line(f"waiting {i}. {fmt.who(w['owner'])}  {fmt.dur(w['waited'])}", {"kind": "waiter"})
    out.append(f"ACTIVITY  last {min(8, len(model['events']))}")
    for e in model["events"][-8:]:
        line(f"{fmt.clock(e['ts'])}  {event_text(e)}")
    out.append(f"PORTS  {len(model['ports'])} dev (+{model['claude_ports']} Claude, "
               f"{model['hidden_ports'] - model['claude_ports']} other hidden)")
    for p in model["ports"]:
        line(f":{p['port']:<6} {p['cmd']:<12} pid {p['pid']}")
    c = model["containers"]
    out.append("CONTAINERS  docker not available" if c is None else f"CONTAINERS  {len(c)} running (never stopped)")
    for row in c or []:
        line("  ".join(row[:3])[:76])
    out.append("MACHINE")
    load = m.get("load")
    line(("load " + " ".join(f"{x:.1f}" for x in load) + f" ({m.get('cores')} cores)") if load else "load ?")
    mem = f"mem free {m['mem_free']:.1f}/{m['mem_total']:.0f} GB" if m.get("mem_total") else "mem ?"
    sw = f"swap used {m['swap_used']:.1f}/{m['swap_total']:.1f} GB" if m.get("swap_total") is not None else "swap ?"
    line(f"{mem}  {sw}" + ("  THRASHING" if model["level"] == "hot" else ""))
    return "\n".join(out)


def summary(model: dict) -> str:
    parts = [f"slots {time.strftime('%H:%M:%S', time.localtime(model['now']))}"]
    for r in model["resources"]:
        if r["kind"] == "pool":
            parts.append(f"{r['name']} {r['in_use']}/{r['size']}" + (f" +{len(r['queue'])} wait" if r["queue"] else ""))
    m = model["machine"]
    if m.get("load"):
        parts.append(f"load {m['load'][0]:.1f}/{m.get('cores')}")
    if m.get("mem_total"):
        parts.append(f"mem {m['mem_free']:.1f}/{m['mem_total']:.0f}G")
    if model["needs"]:
        parts.append(f"{len(model['needs'])} need you")
    return " | ".join(parts)


# ---- actions -------------------------------------------------------------------------------------

class Actions:
    """What the dashboard can do. Each returns a one-line result for the status bar."""

    def __init__(self, ctx):
        from slots import cli
        self.ctx, self.cli = ctx, cli
        self.by = {"lane": "admin", "label": f"dashboard ({os.environ.get('USER', '?')})", "pid": os.getpid()}

    def _call(self, msg: dict) -> dict:
        reply = self.cli.ctx_call(self.ctx, msg)
        if reply is None:
            raise RuntimeError("the daemon can't be reached")
        if reply.get("ev") == "error":
            raise RuntimeError(reply["message"])
        return reply

    def release(self, resource: dict, lease: dict, reason: str = "dashboard") -> str:
        reply = self._call({"op": "release", "admin": True, "lease_id": lease["id"], "resources": [resource["name"]],
                            "owner": self.by, "reason": reason})
        if not reply.get("leases"):
            return "already released"
        what = f"{resource['name']} slot {lease['unit']}" if resource["kind"] == "pool" else resource["name"]
        return f"released {what} ({fmt.who(lease['owner'])}); its processes keep running"

    def dequeue(self, waiter: dict) -> str:
        reply = self._call({"op": "dequeue", "token": waiter["token"], "owner": self.by})
        return f"removed {fmt.who(waiter['owner'])} from the queue" if reply.get("count") else "no longer waiting"

    def promote(self, waiter: dict) -> str:
        self._call({"op": "promote", "token": waiter["token"], "owner": self.by})
        return f"moved {fmt.who(waiter['owner'])} to the front of the line"

    def resize(self, resource: dict, delta: int) -> str:
        if self.ctx.config_path is None:
            raise RuntimeError("no config file to change")
        size = resource["size"] + delta
        change = config_edit.set_pool_size(self.ctx.config_path, resource["name"], size, self.ctx.cfg.device)
        return f"{change} (the daemon picks it up within 2 s)"

    def kill(self, pids: list[int]) -> str:
        done = []
        for p in pids:
            try:
                os.kill(p, signal.SIGTERM)
                done.append(p)
            except OSError:
                pass
        return f"sent SIGTERM to {len(done)} of {len(pids)} processes"


def kill_targets(lane: dict, mach: dict, cfg: config.Config) -> list[int]:
    """A lane's processes that may be stopped: never Docker's, a protected port's, a Claude Code
    session's, or this process."""
    procs = mach.get("procs", {})
    protected_ports = set(cfg.protect.get("ports", []))
    on_protected = {p["pid"] for p in mach.get("ports", []) if p["port"] in protected_ports}
    out = []
    for pid in lane.get("pids", []):
        p = procs.get(pid)
        if not p or pid == os.getpid() or pid in on_protected:
            continue
        exe = p["cmd"].split()[0] if p["cmd"] else ""
        if DOCKER_CMD.search(exe) or CLAUDE_CMD.match(os.path.basename(exe)):
            continue
        out.append(pid)
    return sorted(out)
