"""Plain-text renderings shared by the CLI (and later the dashboard)."""
from __future__ import annotations

import time


def dur(secs: float) -> str:
    secs = int(max(0, secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{secs % 3600 // 60:02d}m"
    return f"{secs // 86400}d{secs % 86400 // 3600}h"


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def who(owner: dict) -> str:
    """Lane first, then the label when it says more, then the session."""
    lane, label = owner.get("lane"), owner.get("label")
    text = label if label and (not lane or label.startswith(lane)) else " ".join(x for x in (lane, label) if x)
    text = text or "someone"
    if owner.get("session"):
        text += f" [session {owner['session'][:8]}]"
    return text


def holder(h: dict) -> str:
    return f"{who(h['owner'])} since {clock(h['since'])}"


def waiting_line(msg: dict) -> str:
    """For a "queued" push: what we're waiting for and on whom."""
    parts = []
    for name in msg["resources"]:
        used, size = msg["in_use"][name]
        holders = [holder(h) for h in msg["holders"] if h["resource"] == name]
        parts.append(f"{name} ({used}/{size} in use: {'; '.join(holders) or 'reserved for earlier waiters'})")
    pos = msg["position"]
    place = "you're next" if pos == 1 else f"#{pos} in line"
    return f"waiting for {', '.join(parts)}; {place}"


def status_text(state: dict) -> str:
    now = state["now"]
    lines = []
    for r in state["resources"]:
        name, size, used = r["name"], r["size"], r["in_use"]
        kind = "pool" if r["kind"] == "pool" else "lock"
        head = f"{name}  {kind}  "
        head += f"{used}/{size} in use" if kind == "pool" else ("held" if used else "free")
        if used > size:
            head += f" (over the limit of {size} since a config change)"
        lines.append(head)
        for h in r["holders"]:
            tag = f"  #{h['unit']}" if kind == "pool" else "  "
            extra = []
            if h["kind"] == "manual":
                extra.append("no pid")
            if h.get("orphan_until"):
                extra.append("reconnecting")
            note = f"  ({', '.join(extra)})" if extra else ""
            lines.append(f"{tag}  {who(h['owner'])}  since {clock(h['since'])}  {dur(now - h['since'])}{note}")
        for i, w in enumerate(r["queue"], 1):
            lines.append(f"    waiting {i}. {who(w['owner'])}  {dur(w['waited'])}")
    d = state.get("daemon", {})
    if d:
        dev = f", device {d['device']}" if d.get("device") else ""
        lines.append(f"(daemon pid {d['pid']}, up {dur(now - d['since'])}{dev}, config {d['config']})")
    return "\n".join(lines)
