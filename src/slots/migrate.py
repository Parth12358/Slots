"""`slots migrate --from <old root>`: adopt what the old scripts hold, without touching their files.

Reads the old layout:
  <root>/.agent-slots/slot-<n>/{owner,since}   agent slots (no pid: imported as manual leases)
  <root>/.agent-slots/e2e/{owner,since}        the e2e lock (no pid either)
  <root>/.agent-slots/e2e-queue                the e2e queue (reported; see below)
  <locks>/<name>.lock.holder                   test slots and exclusive locks: "<label> (pid N, since HH:MM:SSZ)"
  <locks>/.started.*                           run markers (reported, ignored)
where <locks> is <root>/ta-e2e/.locks unless --locks says otherwise.

Holders whose pid is alive become pid leases (they end when that process does); dead ones are
dropped. Nothing changes without --apply, and the old files are never edited or deleted.
"""
from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from slots import config, proc

HOLDER_RE = re.compile(r"^(?P<label>.*) \(pid (?P<pid>\d+), since (?P<since>[^)]*)\)\s*$")


@dataclass
class Item:
    action: str            # "import", "drop", "note"
    resource: str
    who: str
    detail: str
    source: str
    lease: dict | None = field(default=None, repr=False)


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def parse_minute(text: str | None) -> float | None:
    """agent-slot.sh's since: 2026-11-15T14:02Z (UTC)."""
    try:
        return float(calendar.timegm(time.strptime(text or "", "%Y-%m-%dT%H:%MZ")))
    except ValueError:
        return None


def parse_clock(text: str, now: float) -> float:
    """lib-slot.sh's since: HH:MM:SSZ today (UTC), or yesterday if that would be in the future."""
    try:
        h, m, s = (int(x) for x in text.rstrip("Z").split(":"))
    except ValueError:
        return now
    day = time.gmtime(now)
    ts = calendar.timegm((day.tm_year, day.tm_mon, day.tm_mday, h, m, s, 0, 0, 0))
    return ts - 86400 if ts > now + 60 else ts


def plan(root: Path, cfg: config.Config, locks: Path | None = None, now: float | None = None) -> list[Item]:
    now = now or time.time()
    locks = locks or root / "ta-e2e" / ".locks"
    agent_dir = root / ".agent-slots"
    pool, e2e, test = cfg.compat["agent_pool"], cfg.compat["e2e_lock"], cfg.compat["test_pool"]
    items: list[Item] = []

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)

    for d in sorted(agent_dir.glob("slot-*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0):
        if not d.is_dir():
            continue
        owner = _read(d / "owner")
        since = parse_minute(_read(d / "since"))
        if not owner:
            items.append(Item("drop", pool, "?", "no owner file (a half-taken slot)", rel(d)))
            continue
        items.append(Item("import", pool, owner, "no pid recorded: a manual lease until released",
                          rel(d), {"resource": pool, "kind": "manual", "since": since or now,
                                   "owner": {"lane": owner, "label": owner}}))

    e2e_dir = agent_dir / "e2e"
    if e2e_dir.is_dir():
        owner = _read(e2e_dir / "owner")
        if owner:
            items.append(Item("import", e2e, owner, "no pid recorded: a manual lease until released", rel(e2e_dir),
                              {"resource": e2e, "kind": "manual", "since": parse_minute(_read(e2e_dir / "since")) or now,
                               "owner": {"lane": owner, "label": owner}}))
        else:
            items.append(Item("drop", e2e, "?", "no owner file", rel(e2e_dir)))

    queue = [t for t in (_read(agent_dir / "e2e-queue") or "").split() if t]
    if queue:
        items.append(Item("note", e2e + " queue", " ".join(queue),
                          "not imported: those lanes' old --wait loops poll the old files. Once the shims are in "
                          "place, rerun `agent-slot.sh e2e-acquire <ticket> --wait` in this order",
                          rel(agent_dir / "e2e-queue")))

    for f in sorted(locks.glob("*.lock.holder")):
        name = f.name[: -len(".lock.holder")]
        text = _read(f) or ""
        m = HOLDER_RE.match(text)
        resource = test if re.fullmatch(r"slot-\d+", name) else name
        if not m:
            items.append(Item("drop", resource, text or "?", "unreadable holder line", rel(f)))
            continue
        label, pid = m["label"], int(m["pid"])
        if resource != test:
            try:
                resource = cfg.resolve(name).name
            except config.ConfigError:
                items.append(Item("note", name, label, "not imported: no such lock in the config", rel(f)))
                continue
        if not proc.exists(pid):
            items.append(Item("drop", resource, label, f"pid {pid} is gone (stale holder file)", rel(f)))
            continue
        lane = label.split()[0] if label.split() and cfg.lane_pattern.search(label.split()[0]) else None
        items.append(Item("import", resource, label, f"pid {pid} is running: ends when it exits", rel(f),
                          {"resource": resource, "kind": "pid", "since": parse_clock(m["since"], now),
                           "owner": {"lane": lane, "label": label, "pid": pid}}))

    for f in sorted(locks.glob(".started.*")):
        items.append(Item("note", "-", f.name, "leftover run marker; ignored", rel(f)))
    for f in sorted(agent_dir.glob("signin-failed-*")) + [agent_dir / "vault-ok"]:
        if f.exists():
            items.append(Item("note", "-", f.name, "watcher state; the watchers step replaces it", rel(f)))
    return items


def render(items: list[Item]) -> str:
    if not items:
        return "nothing to migrate"
    w_res = max(len(i.resource) for i in items)
    w_who = max(len(i.who) for i in items)
    return "\n".join(f"{i.action:<7} {i.resource:<{w_res}}  {i.who:<{w_who}}  {i.detail}  [{i.source}]" for i in items)
