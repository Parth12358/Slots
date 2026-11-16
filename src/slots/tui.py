"""The live dashboard (curses): your point of contact with slots.

Rendering follows the reference dashboard: ncurses line drawing (ACS) and coloured
spaces by default, which render right on any curses build; rounded corners and
block glyphs only with --unicode and only when a startup check shows curses
advances one column per glyph. A solid dark background by default, or the
terminal's own with --transparent. Every action asks y/N first.
"""
from __future__ import annotations

import locale
import os
import time
from collections import deque

from slots import dashboard, fmt, machine

REFRESH = 2.0
TWO_COL_W = 140
DETAIL_MIN_H = 30
DETAIL_H = 7

HELP = ["j/k or arrows  move     enter  fix the selected Needs-you item",
        "r  release a lease       e  remove a waiter from its queue      u  move a waiter to the front",
        "+/-  grow or shrink the selected pool (writes the config)       c  clear a lease whose holder is gone",
        "K  SIGTERM the selected lane's processes (shows them first)     d  detail panel (30+ rows)",
        "q  quit      ?  this help        every action asks y/N and is logged by the daemon"]

KEYS = [("j/k", "move", None), ("enter", "fix", "enter"), ("r", "release", "r"), ("e", "dequeue", "e"),
        ("u", "to front", "u"), ("+/-", "pool size", "+"), ("K", "stop procs", "K"), ("d", "detail", None),
        ("?", "help", None), ("q", "quit", None)]
SHORT = {"j/k": "mv", "enter": "fix", "r": "rel", "e": "deq", "u": "1st", "+/-": "size", "K": "stop",
         "d": "det", "?": "help", "q": "quit"}

HIST_LEN = 60
GLYPHS_UTF8 = dict(tl="╭", tr="╮", bl="╰", br="╯", h="─", v="│", full="█", parts=" ▏▎▍▌▋▊▉", empty="░",
                   spark=" ▁▂▃▄▅▆▇█", alive="●", stale="○", dot="·", acs=False)
GLYPHS_ACS = dict(tl="+", tr="+", bl="+", br="+", h="-", v="|", full=" ", parts="        ", empty=" ",
                  spark=" _.-=#", alive="*", stale="o", dot="-", acs=True)


def utf8_ok() -> bool:
    try:
        codeset = locale.nl_langinfo(locale.CODESET)
    except (AttributeError, ValueError):
        codeset = locale.getpreferredencoding(False)
    return "UTF8" in (codeset or "").upper().replace("-", "")


class Palette:
    """Colour pairs on demand: 256-colour names and gradients when the terminal has them, else 8 colours."""

    def __init__(self, curses, solid=True):
        self.c = curses
        self.on = curses.has_colors()
        self.rich = self.on and curses.COLORS >= 256
        self.max_pairs = (curses.COLOR_PAIRS - 1) if self.on else 0
        self.pairs = {}
        C = curses
        if self.rich:
            self.names = dict(green=114, yellow=221, red=203, blue=75, purple=141, gold=179, cyan=81,
                              pink=211, grey=245, dark=239, white=255, selbg=237)
            self.grad = [46, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196]
        else:
            self.names = dict(green=C.COLOR_GREEN, yellow=C.COLOR_YELLOW, red=C.COLOR_RED, blue=C.COLOR_BLUE,
                              purple=C.COLOR_MAGENTA, gold=C.COLOR_YELLOW, cyan=C.COLOR_CYAN,
                              pink=C.COLOR_MAGENTA, grey=C.COLOR_WHITE, dark=C.COLOR_WHITE, white=C.COLOR_WHITE,
                              selbg=-1)
            self.grad = [C.COLOR_GREEN] * 4 + [C.COLOR_YELLOW] * 4 + [C.COLOR_RED] * 3
        self.bg = (233 if self.rich else C.COLOR_BLACK) if (solid and self.on) else -1
        self.trough = 236 if self.rich else -1

    def pair(self, fg, bg=-1):
        if not self.on or (fg == -1 and bg == -1):
            return 0
        key = (fg, bg)
        if key not in self.pairs:
            n = len(self.pairs) + 1
            if n > self.max_pairs:
                return 0
            try:
                self.c.init_pair(n, fg, bg)
            except self.c.error:
                return 0
            self.pairs[key] = n
        return self.c.color_pair(self.pairs[key])

    def a(self, name, extra=0, bg=None):
        return self.pair(self.names.get(name, -1) if name else -1, self.bg if bg is None else bg) | extra

    def grad_color(self, frac):
        return self.grad[max(0, min(len(self.grad) - 1, int(frac * (len(self.grad) - 1) + 0.5)))]

    def grad_attr(self, frac, extra=0, bg=None):
        return self.pair(self.grad_color(frac), self.bg if bg is None else bg) | extra

    def grad_bg(self, frac, extra=0):
        return self.pair(self.names["white"], self.grad_color(frac)) | extra

    def base(self):
        return self.pair(-1, self.bg) or self.pair(self.names["white"], self.bg)


# ---- panels --------------------------------------------------------------------------------------

def pools(model):
    return [r for r in model["resources"] if r["kind"] == "pool"]


def build_panels(model: dict, inner: int, G: dict) -> dict:
    """id -> {title, hint, accent, lines: [(segments, action)]}. Segments: ("t", text, style) |
    ("m", fraction, width) meter."""
    m = model["machine"]
    cores = m.get("cores") or 1
    tot_kb = (m.get("mem_total") or 0) * 2**20
    mw = 10 if inner >= 90 else 6 if inner >= 64 else 0
    now = model["now"]
    P = {}

    def meter(frac, text, style="norm"):
        return ([("m", frac, mw), ("t", " ", "norm")] if mw else []) + [("t", text, style)]

    def mark(kind):
        return ("t", G[kind] + " ", "alive" if kind == "alive" else "stale")

    def proc_cols(cpu, rss):
        return (meter(cpu / (100 * cores), f"{cpu:>4.0f}%  ", "hot" if cpu >= dashboard.HOT_CPU else "norm") +
                meter(rss / tot_kb if tot_kb else 0, f"{dashboard.mb(rss):>6}  "))

    lines = []
    for n in model["needs"]:
        style = {"hot": "hot", "warn": "stale", "note": "dim"}[n["level"]]
        segs = [("t", "! " if n["level"] != "note" else "  ", style), ("t", n["text"], style)]
        if n.get("fix"):
            segs.append(("t", f"  [enter: {n['fix_label']}]", "dim"))
        lines.append((segs, {**n["fix"], "kind": "need", "need": n} if n.get("fix") else None))
    if lines:
        P["needs"] = dict(title=f"needs you {len(lines)}", hint="enter fix", accent="red"
                          if any(n["level"] == "hot" for n in model["needs"]) else "yellow", lines=lines)

    lines = []
    idle_flags = {id(n["fix"]["lease"]) for n in model["needs"] if n.get("fix") and n["fix"].get("lease")}
    for ln in model["lanes"]:
        held = ", ".join(f"{r['name']}#{h['unit']}" if r["kind"] == "pool" else r["name"] for r, h in ln["leases"])
        stale = any(id(h) in idle_flags for _, h in ln["leases"])
        segs = [mark("stale" if stale else "alive"), ("t", f"{ln['name'][:10]:<10} ", "owner"),
                ("t", f"{held[:22]:<22} ", "dim")]
        if ln["pids"]:
            segs += proc_cols(ln["cpu"], ln["rss"]) + [("t", f"{len(ln['pids'])}p" + ("~" if ln["fallback"] else "")
                                                         + " ", "dim")]
            if ln["ports"]:
                segs.append(("t", " ".join(f":{p}" for p in ln["ports"][:3]), "@cyan"))
        else:
            segs.append(("t", "idle? no processes" if stale else "no processes", "stale" if stale else "dim"))
        if ln["waiting"]:
            segs.append(("t", "  waiting " + ",".join(",".join(w["resources"]) for w in ln["waiting"]), "wait"))
        lines.append((segs, {"kind": "lane", "lane": ln}))
    P["lanes"] = dict(title=f"lanes {len(model['lanes'])}", hint="K stop", accent="green", lines=lines)

    lines = []
    for r in model["resources"]:
        if r["kind"] == "pool":
            frac = r["in_use"] / r["size"] if r["size"] else 1
            segs = [("t", f"{r['name'][:12]:<12} ", "@blue")] + meter(frac, f"{r['in_use']}/{r['size']}", "norm")
            if r["queue"]:
                segs.append(("t", f"  +{len(r['queue'])} waiting", "wait"))
            if r.get("ttl"):
                segs.append(("t", f"  ttl {fmt.dur(r['ttl'])}", "dim"))
            lines.append((segs, {"kind": "pool", "resource": r}))
        else:
            segs = [("t", f"{r['name'][:24]:<24} ", "@gold"),
                    ("t", "held" if r["in_use"] else "free", "norm" if r["in_use"] else "dim")]
            if r["queue"]:
                segs.append(("t", f"  +{len(r['queue'])} waiting", "wait"))
            lines.append((segs, None))
        for h in r["holders"]:
            kind = {"conn": "run", "pid": "pid", "manual": "no pid"}[h["kind"]]
            flagged = id(h) in idle_flags
            segs = [("t", "  ", "norm"), mark("stale" if flagged else "alive"),
                    ("t", f"#{h['unit']} " if r["kind"] == "pool" else "", "dim"),
                    ("t", f"{fmt.who(h['owner'])[:30]:<30} ", "owner"),
                    ("t", f"{fmt.dur(now - h['since']):>6}  {kind}", "stale" if flagged else "dim")]
            lines.append((segs, {"kind": "lease", "resource": r, "lease": h}))
        for i, w in enumerate(r["queue"], 1):
            lines.append(([("t", "    ", "norm"), ("t", f"{i}. ", "wait"), ("t", f"{fmt.who(w['owner'])[:30]:<30} ", "owner"),
                           ("t", f"waiting {fmt.dur(w['waited'])}", "wait")],
                          {"kind": "waiter", "resource": r, "waiter": w}))
    busy = sum(r["in_use"] for r in model["resources"])
    P["resources"] = dict(title=f"resources {G['dot']} {busy} held", hint="r release", accent="blue", lines=lines)

    lines = [([("t", f"{fmt.clock(e['ts'])} ", "dim"), ("t", dashboard.event_text(e)[:inner - 6], "norm")], None)
             for e in reversed(model["events"][-12:])]
    P["activity"] = dict(title="activity", hint="", accent="purple", lines=lines)

    lines = [([("t", "  ", "norm"), ("t", f":{p['port']:<6}", "@cyan"), ("t", f"{p['cmd']:<11}", "norm"),
               ("t", f"pid {p['pid']}", "dim")], None) for p in model["ports"]]
    for c in model["containers"] or []:
        lines.append(([("t", "  ", "norm"), ("t", f"{c[0][:18]:<19}", "owner"), ("t", "  ".join(c[1:3]), "dim")], None))
    if model["containers"] is None:
        lines.append(([("t", "  docker not available", "dim")], None))
    lines.append(([("t", f"  +{model['claude_ports']} Claude, {model['hidden_ports'] - model['claude_ports']} "
                         "other listeners hidden", "dim")], None))
    P["ports"] = dict(title=f"ports {len(model['ports'])} {G['dot']} containers {len(model['containers'] or [])}",
                      hint="", accent="cyan", lines=lines)
    return P


def details(model: dict, a: dict | None) -> list[str]:
    if not a:
        return ["(select a row with j/k)"]
    procs, out = model["machine"].get("procs", {}), []

    def plist(pids, fallback=()):
        for p in sorted(pids):
            pr = procs.get(p)
            if pr:
                how = "~" if p in fallback else " "
                out.append(f" {how}{p:>6} {pr['cpu']:>5.1f}% {dashboard.mb(pr['rss']):>6} {pr['etime']:>11}  {pr['cmd']}")

    k = a["kind"]
    if k in ("lease", "need") and a.get("lease"):
        r, h = a["resource"], a["lease"]
        o = h["owner"]
        out.append(f"{r['name']}" + (f" slot {h['unit']}" if r["kind"] == "pool" else "") +
                   f": {fmt.who(o)}  since {fmt.clock(h['since'])} ({fmt.dur(model['now'] - h['since'])})")
        out.append(f"kind {h['kind']}  pid {o.get('pid') or '-'}  session {o.get('session') or '-'}  "
                   f"host {o.get('host') or '-'}  last activity {fmt.dur(model['now'] - (h.get('last_active') or h['since']))} ago")
        ln = next((l for l in model["lanes"] if l["name"] == (o.get("lane") or o.get("label"))), None)
        if ln:
            plist(ln["pids"], ln["fallback"])
    elif k in ("waiter", "need") and a.get("waiter"):
        w = a["waiter"]
        out.append(f"{fmt.who(w['owner'])} waiting {fmt.dur(w['waited'])} for {', '.join(w['resources'])}")
        out.append(f"held by: " + "; ".join(fmt.who(h["owner"]) for h in a["resource"]["holders"]) or "-")
    elif k == "lane":
        ln = a["lane"]
        out.append(f"lane {ln['name']}: {len(ln['pids'])} procs  {ln['cpu']:.0f}% cpu  {dashboard.mb(ln['rss'])}  "
                   f"ports {' '.join(f':{p}' for p in ln['ports']) or 'none'}   (~ = found by folder, not tracked)")
        plist(ln["pids"], ln["fallback"])
    elif k == "pool":
        r = a["resource"]
        out.append(f"pool {r['name']}: {r['in_use']}/{r['size']} in use, {len(r['queue'])} waiting, hold {r['hold']}")
        out.append("+ and - change its size in the config file (asks first); the daemon reloads it")
    return out


def applicable(a: dict | None) -> set[str]:
    if not a:
        return set()
    keys = set()
    if a["kind"] == "need":
        keys.add("enter")
    if a.get("lease"):
        keys |= {"r", "c"}
    if a.get("waiter"):
        keys |= {"e", "u"}
    if a["kind"] == "pool":
        keys.add("+")
    if a["kind"] == "lane" and a["lane"]["pids"]:
        keys.add("K")
    return keys


# ---- the live view -------------------------------------------------------------------------------

def run(ctx, glyphs: str = "acs", bg: str = "solid") -> int:
    import curses
    locale.setlocale(locale.LC_ALL, "")
    return curses.wrapper(lambda stdscr: live(stdscr, ctx, glyphs, bg)) or 0


def collect(ctx) -> dict:
    from slots import cli
    reply = cli.ctx_call(ctx, {"op": "status"})
    state = reply["state"] if reply else {"resources": [], "events": [], "now": time.time()}
    mach = machine.collect(str(ctx.cfg.lanes_root) if ctx.cfg.lanes_root else None)
    model = dashboard.build(state, mach, ctx.cfg, config_path=str(ctx.config_path) if ctx.config_path else None)
    if reply is None:
        model["needs"].insert(0, {"level": "hot", "text": "the slots daemon can't be reached", "fix": None})
    return model


def one_column_glyphs(stdscr) -> bool:
    """True when curses advances exactly one column per Unicode glyph."""
    import curses
    ok = True
    try:
        G = GLYPHS_UTF8
        for ch in G["tl"] + G["v"] + G["full"] + G["empty"] + G["alive"] + G["parts"][1] + G["spark"][1] + G["dot"]:
            stdscr.addstr(0, 0, ch)
            if stdscr.getyx() != (0, 1):
                ok = False
                break
            stdscr.addnstr(0, 0, ch * 3, 1)
            if stdscr.getyx() != (0, 1):
                ok = False
                break
    except (curses.error, UnicodeError):
        ok = False
    stdscr.erase()
    return ok


def live(stdscr, ctx, glyphs: str, bg: str):
    import curses
    G = GLYPHS_UTF8 if (glyphs == "unicode" and utf8_ok() and one_column_glyphs(stdscr)) else GLYPHS_ACS
    safe = G["acs"]
    acts = dashboard.Actions(ctx)
    hist = {k: deque(maxlen=HIST_LEN) for k in ("load", "ram", "swap", "pool")}
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    pal = Palette(curses, solid=bg != "transparent")
    BOLD, DIM, REV = curses.A_BOLD, curses.A_DIM, curses.A_REVERSE
    if pal.bg != -1:
        try:
            stdscr.bkgd(" ", pal.base())
        except curses.error:
            pass
    data, last, sel, msg = None, 0.0, 0, ""
    show_help, show_detail = False, True
    tops = [0, 0]

    def put(y, x, text, attr=0, maxw=None):
        h, w = stdscr.getmaxyx()
        if 0 <= y < h and 0 <= x < w and text:
            n = max(0, w - x - (1 if y == h - 1 else 0))
            if maxw is not None:
                n = min(n, maxw)
            if n <= 0:
                return
            if safe:
                text = text.encode("ascii", "replace").decode("ascii")
            try:
                stdscr.addnstr(y, x, text, n, attr)
            except curses.error:
                pass

    def acs(y, x, ch, attr=0):
        h, w = stdscr.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            try:
                stdscr.addch(y, x, ch, attr)
            except curses.error:
                pass

    def hline(y, x, n, attr):
        h, w = stdscr.getmaxyx()
        n = min(n, w - x)
        if 0 <= y < h and 0 <= x < w and n > 0:
            if safe:
                try:
                    stdscr.hline(y, x, curses.ACS_HLINE | attr, n)
                except curses.error:
                    pass
            else:
                put(y, x, G["h"] * n, attr, n)

    def style(name, selected):
        bgc = pal.names["selbg"] if (selected and pal.rich) else pal.bg
        rev = REV if (selected and not pal.rich) else 0
        if name.startswith("@"):
            return pal.a(name[1:], BOLD | rev, bgc)
        fg, extra = {"norm": (None, 0), "dim": ("grey", 0 if pal.rich else DIM), "stale": ("yellow", 0),
                     "hot": ("red", BOLD), "wait": ("yellow", BOLD), "alive": ("green", BOLD),
                     "owner": ("white", BOLD)}.get(name, (None, 0))
        return pal.a(fg, extra | rev, bgc)

    def segs_draw(y, x, width, segs, selected=False):
        end = x + width
        if selected:
            put(y, x, " " * width, style("norm", True), width)
        bgc = pal.names["selbg"] if (selected and pal.rich) else pal.bg
        rev = REV if (selected and not pal.rich) else 0
        cx = x
        for s in segs:
            if cx >= end:
                break
            if s[0] == "t":
                put(y, cx, s[1], style(s[2], selected), end - cx)
                cx += len(s[1])
            elif s[0] == "m":
                frac, mw = max(0.0, min(1.0, s[1])), min(s[2], end - cx)
                cells = frac * mw
                full, part = int(cells), int((cells - int(cells)) * 8)
                if safe:
                    filled = int(round(cells))
                    for i in range(mw):
                        if i < filled:
                            put(y, cx + i, " ", pal.grad_bg(i / max(1, mw - 1)), 1)
                        elif pal.trough != -1:
                            put(y, cx + i, " ", pal.pair(-1, pal.trough) or pal.a("dark", REV), 1)
                        else:
                            acs(y, cx + i, curses.ACS_CKBOARD, pal.a("dark", DIM))
                    cx += mw
                    continue
                for i in range(mw):
                    g = pal.grad_attr(i / max(1, mw - 1), rev, bgc)
                    if i < full:
                        put(y, cx + i, G["full"], g, 1)
                    elif i == full and part and G["parts"][part] != " ":
                        put(y, cx + i, G["parts"][part], g, 1)
                    else:
                        put(y, cx + i, G["empty"], pal.a("dark", 0 if pal.rich else DIM, bgc) | rev, 1)
                cx += mw
            elif s[0] == "s":
                vals, sw = list(s[1]), min(s[2], end - cx)
                vals = vals[-sw:] if sw > 0 else []
                pad = sw - len(vals)
                top = len(G["spark"]) - 1
                for i, v in enumerate(vals):
                    lvl = int(round(max(0.0, min(1.0, v)) * top))
                    if lvl:
                        put(y, cx + pad + i, G["spark"][lvl], pal.grad_attr(v, rev, bgc), 1)
                cx += sw

    def graph(y, x, width, height, seq):
        vals = list(seq)[-width:] if width > 0 else []
        pad = width - len(vals)
        if safe:
            for i, v in enumerate(vals):
                for r in range(int(round(max(0.0, min(1.0, v)) * height))):
                    put(y + height - 1 - r, x + pad + i, " ", pal.grad_bg((r + 0.5) / height), 1)
            return
        top = len(G["spark"]) - 1
        for i, v in enumerate(vals):
            total = max(0.0, min(1.0, v)) * height * top
            for r in range(height):
                fill = int(round(max(0.0, min(top, total - r * top))))
                if fill:
                    put(y + height - 1 - r, x + pad + i, G["spark"][fill], pal.grad_attr((r + 0.5) / height), 1)

    def border_attr(accent, active):
        if active:
            return pal.a(accent, BOLD)
        return pal.a("dark") if pal.rich else pal.a(accent, DIM)

    def box_top(y, x, width, title, hint, accent, active):
        if width < 4:
            return
        ba = border_attr(accent, active)
        if safe:
            acs(y, x, curses.ACS_ULCORNER, ba)
            hline(y, x + 1, width - 2, ba)
            acs(y, x + width - 1, curses.ACS_URCORNER, ba)
        else:
            put(y, x, G["tl"] + G["h"] * (width - 2) + G["tr"], ba, width)
        room = width - 6
        t = title[:max(0, room)]
        if t:
            put(y, x + 2, " " + t + " ", pal.a(accent, BOLD), len(t) + 2)
        if hint and room - len(t) - 4 > len(hint):
            hx = x + width - len(hint) - 4
            key, _, rest = hint.partition(" ")
            put(y, hx, " ", ba, 1)
            put(y, hx + 1, key, pal.a(accent, BOLD | curses.A_UNDERLINE), len(key))
            put(y, hx + 1 + len(key), (" " + rest if rest else "") + " ", pal.a("grey", 0 if pal.rich else DIM))

    def box_bottom(y, x, width, accent, active):
        if width < 4:
            return
        ba = border_attr(accent, active)
        if safe:
            acs(y, x, curses.ACS_LLCORNER, ba)
            hline(y, x + 1, width - 2, ba)
            acs(y, x + width - 1, curses.ACS_LRCORNER, ba)
        else:
            put(y, x, G["bl"] + G["h"] * (width - 2) + G["br"], ba, width)

    def box_side(y, x, width, accent, active):
        ba = border_attr(accent, active)
        if safe:
            acs(y, x, curses.ACS_VLINE, ba)
            acs(y, x + width - 1, curses.ACS_VLINE, ba)
        else:
            put(y, x, G["v"], ba, 1)
            put(y, x + width - 1, G["v"], ba, 1)

    def machine_panel(y, x, width, height, d):
        m = d["machine"]
        cores = m.get("cores") or 1
        load = m.get("load") or [0.0, 0.0, 0.0]
        lvl = d["level"]
        slow = f" {G['dot']} refresh {m['took']:.1f}s" if m.get("took", 0) > 1 else ""
        dev = f" {G['dot']} {d['device']}" if d.get("device") else ""
        box_top(y, x, width, "machine " + time.strftime("%H:%M:%S") + dev, f"{cores} cores{slow}", "pink", False)
        for r in range(1, height - 1):
            box_side(y + r, x, width, "pink", False)
        box_bottom(y + height - 1, x, width, "pink", False)
        inner_w, inner_h = width - 4, height - 2
        tot, free = m.get("mem_total"), m.get("mem_free")
        ram = (1 - free / tot) if tot else 0.0
        swf = (m["swap_used"] / m["swap_total"]) if m.get("swap_total") else 0.0
        cpu_txt = f"{load[0] / cores * 100:>4.0f}%"
        ram_txt = f"{tot - free:.1f}/{tot:.0f}G" if tot else "?"
        sw_txt = (f"{m['swap_used']:.1f}/{m['swap_total']:.1f}G" if m.get("swap_total") is not None else "?") + \
                 (" thrashing" if lvl == "hot" else "")
        sw_style = "hot" if lvl == "hot" else "stale" if lvl == "warn" else "norm"
        rows_ = [("cpu ", load[0] / cores, cpu_txt + f" of {cores}", "hot" if load[0] > cores else "norm", "load"),
                 ("ram ", ram, ram_txt, "norm", "ram"), ("swap", swf, sw_txt, sw_style, "swap")]
        for i, r in enumerate(pools(d)[:2]):
            rows_.append((f"{r['name'][:4]:<4}", r["in_use"] / r["size"] if r["size"] else 1,
                          f"{r['in_use']}/{r['size']}" + (f" +{len(r['queue'])} wait" if r["queue"] else ""),
                          "wait" if r["queue"] else "norm", "pool" if i == 0 else None))
        if inner_h >= 5:
            gw = max(16, inner_w * 2 // 5)
            graph(y + 1, x + 2, gw, inner_h, hist["load"])
            put(y + 1, x + 3, f" load {load[0]:.1f} {load[1]:.1f} {load[2]:.1f} ", pal.a("white", BOLD))
            mx, mwid = x + 2 + gw + 2, inner_w - gw - 2
            bar = max(6, min(24, mwid // 3))
            spark_w = max(0, mwid - 6 - bar - 1 - 18)
            for i, (label, frac, txt, st, h_) in enumerate(rows_[:inner_h]):
                segs = [("t", f"{label}  ", "@pink"), ("m", frac, bar), ("t", f" {txt[:17]:<17} ", st)]
                if h_ and spark_w:
                    segs.append(("s", hist[h_], spark_w))
                segs_draw(y + 1 + i, mx, mwid, segs)
        else:
            bar = 8 if inner_w >= 96 else 5
            line1 = []
            for label, frac, txt, st, _ in rows_[:3]:
                line1 += [("t", f"{label.strip()} ", "@pink"), ("m", frac, bar), ("t", f" {txt}  ", st)]
            line2 = [("t", "load ", "@pink"), ("s", hist["load"], 14), ("t", f" {load[0]:.1f}  ", "norm")]
            for label, frac, txt, st, _ in rows_[3:]:
                line2 += [("t", f"{label.strip()} ", "@pink"), ("m", frac, bar), ("t", f" {txt}  ", st)]
            for i, segs in enumerate((line1, line2)[:inner_h]):
                segs_draw(y + 1 + i, x + 2, inner_w, segs)

    def ask(question):
        h, w = stdscr.getmaxyx()
        put(h - 1, 0, " " * (w - 1), REV)
        put(h - 1, 0, " " + question[: w - 12] + " [y/N] ", REV | BOLD)
        stdscr.timeout(-1)
        return stdscr.getch() in (ord("y"), ord("Y"))

    def record(d):
        m = d["machine"]
        cores = m.get("cores") or 1
        if m.get("load"):
            hist["load"].append(min(1.0, m["load"][0] / cores))
        if m.get("mem_total"):
            hist["ram"].append(max(0.0, min(1.0, 1 - m["mem_free"] / m["mem_total"])))
        if m.get("swap_total"):
            hist["swap"].append(min(1.0, m["swap_used"] / m["swap_total"]))
        ps = pools(d)
        if ps:
            hist["pool"].append(min(1.0, ps[0]["in_use"] / ps[0]["size"]))

    while True:
        if data is None or time.monotonic() - last >= REFRESH:
            data, last = collect(ctx), time.monotonic()
            record(data)
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        if h < 10 or w < 40:
            put(0, 0, "slots: terminal too small (40x10 minimum)", BOLD)
            put(1, 0, dashboard.summary(data))
            put(h - 1, 0, "q quit")
            stdscr.refresh()
            stdscr.timeout(int(max(0.05, REFRESH - (time.monotonic() - last)) * 1000))
            if stdscr.getch() in (ord("q"), 27):
                return 0
            continue

        wide = w >= TWO_COL_W and h >= DETAIL_MIN_H
        mh = 7 if wide else (4 if h >= 16 else 0)
        detail_on = show_detail and h >= DETAIL_MIN_H
        dh = DETAIL_H if detail_on else 0
        body_y, body_h = mh, max(1, h - 1 - mh - dh)
        widths = [w // 2, w - w // 2] if wide else [w]
        xs = [0, widths[0]] if wide else [0]
        panels = build_panels(data, min(widths) - 4, G)
        first = ["needs"] if "needs" in panels else []
        stacks = [first + ["lanes", "resources"], ["activity", "ports"]] if wide else \
            [first + ["lanes", "resources", "activity", "ports"]]
        cols = []
        for stack in stacks:
            vl = []
            for pid in stack:
                vl.append(("top", pid))
                lines = panels[pid]["lines"] or [([("t", "  none", "dim")], None)]
                panels[pid]["lines"] = lines
                vl.extend(("row", pid, i) for i in range(len(lines)))
                vl.append(("bot", pid))
            cols.append(vl)
        order = [(c, j) for c, vl in enumerate(cols) for j, v in enumerate(vl)
                 if v[0] == "row" and panels[v[1]]["lines"][v[2]][1]]
        sel = min(sel, max(0, len(order) - 1))
        cur_pos = order[sel] if order else None
        cur_v = cols[cur_pos[0]][cur_pos[1]] if cur_pos else None
        cur = panels[cur_v[1]]["lines"][cur_v[2]] if cur_v else None
        active = cur_v[1] if cur_v else None

        if mh:
            machine_panel(0, 0, w, mh, data)
        for c, vl in enumerate(cols):
            if cur_pos and cur_pos[0] == c:
                j = cur_pos[1]
                want = j - 1 if j > 0 and vl[j - 1][0] == "top" else j
                if want < tops[c]:
                    tops[c] = want
                elif j >= tops[c] + body_h:
                    tops[c] = j - body_h + 2 if j + 1 < len(vl) else j - body_h + 1
            tops[c] = max(0, min(tops[c], max(0, len(vl) - body_h)))
            cw, cx = widths[c], xs[c]
            for k, v in enumerate(vl[tops[c]:tops[c] + body_h]):
                yy, p = body_y + k, panels[v[1]]
                act_p = v[1] == active
                if v[0] == "top":
                    box_top(yy, cx, cw, p["title"], p["hint"], p["accent"], act_p)
                elif v[0] == "bot":
                    box_bottom(yy, cx, cw, p["accent"], act_p)
                else:
                    box_side(yy, cx, cw, p["accent"], act_p)
                    segs_draw(yy, cx + 2, cw - 4, p["lines"][v[2]][0], cur_pos == (c, tops[c] + k))
            hidden = len(vl) - tops[c] - body_h
            if hidden > 0:
                put(body_y + body_h - 1, cx + cw - 12, f" +{hidden} more ", pal.a("grey", BOLD), 11)
            if tops[c] > 0:
                put(body_y, cx + cw - 12, f" {tops[c]} above ", pal.a("grey", BOLD), 11)

        if detail_on:
            dy = h - 1 - dh
            kind = cur[1]["kind"] if cur and cur[1] else "none"
            box_top(dy, 0, w, f"detail {G['dot']} {kind}", "d hide", "grey" if pal.rich else "white", False)
            for r in range(1, dh - 1):
                box_side(dy + r, 0, w, "white", False)
            box_bottom(dy + dh - 1, 0, w, "white", False)
            for i, line in enumerate(details(data, cur[1] if cur else None)[:dh - 2]):
                put(dy + 1 + i, 2, line, pal.a("white", BOLD) if i == 0 else 0, w - 4)

        if show_help:
            hw_ = min(w - 2, max(len(l) for l in HELP) + 4)
            hy = max(0, h - 1 - len(HELP) - 2 - dh)
            hx = max(0, (w - hw_) // 2)
            box_top(hy, hx, hw_, "help", "? close", "white", True)
            for i, l in enumerate(HELP):
                put(hy + 1 + i, hx, " " * hw_, 0, hw_)
                box_side(hy + 1 + i, hx, hw_, "white", True)
                put(hy + 1 + i, hx + 2, l, 0, hw_ - 4)
            box_bottom(hy + len(HELP) + 1, hx, hw_, "white", True)

        ok = applicable(cur[1] if cur else None)
        bars = [list(KEYS), [(k_, SHORT.get(k_, l_), n_) for k_, l_, n_ in KEYS], [(k_, "", n_) for k_, _, n_ in KEYS]]
        chosen = next((b_ for b_ in bars if sum(len(k_) + len(l_) + 4 for k_, l_, _ in b_) < w - 1), bars[-1])
        x = 0
        for key, label, needs in chosen:
            usable = needs is None or needs in ok
            chip = pal.a("blue", BOLD | REV) if usable else pal.a("dark", 0 if pal.rich else DIM)
            put(h - 1, x, f" {key} ", chip)
            if label:
                put(h - 1, x + len(key) + 2, f" {label} ", 0 if usable else pal.a("dark", 0 if pal.rich else DIM))
            x += len(key) + len(label) + (4 if label else 3)
        note = msg or ("" if mh else dashboard.summary(data))
        if note and x + 2 < w:
            put(h - 1, max(x + 1, w - len(note) - 2), note[:max(0, w - x - 3)], pal.a("yellow" if msg else "grey"))
        stdscr.refresh()

        stdscr.timeout(int(max(0.05, REFRESH - (time.monotonic() - last)) * 1000))
        k = stdscr.getch()
        if k == -1:
            continue
        msg = ""
        if k in (ord("q"), 27):
            return 0
        if k == curses.KEY_RESIZE:
            continue
        if k == ord("?"):
            show_help = not show_help
        elif k == ord("d"):
            show_detail = not show_detail
        elif k in (ord("j"), curses.KEY_DOWN):
            sel = min(sel + 1, max(0, len(order) - 1))
        elif k in (ord("k"), curses.KEY_UP):
            sel = max(sel - 1, 0)
        elif cur is None or cur[1] is None:
            msg = "nothing selected"
        else:
            try:
                msg = act(k, cur[1], data, acts, ask, ctx, curses)
            except Exception as e:   # an action's failure shows in the status bar, never kills the view
                msg = f"failed: {e}"
            data = None


def act(k, a, data, acts, ask, ctx, curses) -> str:
    """One key on the selected row. Returns the status-bar message."""
    def what(r, h):
        return (f"{r['name']} slot {h['unit']}" if r["kind"] == "pool" else r["name"]) + f" held by {fmt.who(h['owner'])}"

    if k in (10, 13, curses.KEY_ENTER):
        if a["kind"] != "need":
            return "enter fixes a Needs-you item"
        n = a["need"]
        if n.get("fix_label") == "release" and ask(f"Release {what(a['resource'], a['lease'])}? Its processes keep running."):
            return acts.release(a["resource"], a["lease"], "needs-you: " + n["text"][:60])
        if n.get("fix_label") == "move to front" and ask(f"Move {fmt.who(a['waiter']['owner'])} to the front?"):
            return acts.promote(a["waiter"])
        return ""
    if k == ord("r"):
        if not a.get("lease"):
            return "r works on a lease (a holder row)"
        if ask(f"Release {what(a['resource'], a['lease'])}? Its processes keep running."):
            return acts.release(a["resource"], a["lease"])
        return ""
    if k == ord("c"):
        if not a.get("lease"):
            return "c works on a lease"
        o = a["lease"]["owner"]
        if o.get("pid") and machine_alive(o["pid"]):
            return "refused: the holder's pid is alive (use r to release it anyway)"
        if ask(f"Clear {what(a['resource'], a['lease'])}? Its holder is gone."):
            return acts.release(a["resource"], a["lease"], "stale")
        return ""
    if k == ord("e"):
        if not a.get("waiter"):
            return "e works on a waiting row"
        if ask(f"Remove {fmt.who(a['waiter']['owner'])} from the {a['resource']['name']} queue?"):
            return acts.dequeue(a["waiter"])
        return ""
    if k == ord("u"):
        if not a.get("waiter"):
            return "u works on a waiting row"
        if ask(f"Move {fmt.who(a['waiter']['owner'])} to the front of the line?"):
            return acts.promote(a["waiter"])
        return ""
    if k in (ord("+"), ord("="), ord("-")):
        if a["kind"] != "pool":
            return "+ and - work on a pool row"
        r = a["resource"]
        delta = -1 if k == ord("-") else 1
        if r["size"] + delta < 1:
            return "a pool needs at least 1 slot"
        if ask(f"Change {r['name']} from {r['size']} to {r['size'] + delta} slots in {ctx.config_path}?"):
            return acts.resize(r, delta)
        return ""
    if k == ord("K"):
        if a["kind"] != "lane":
            return "K works on a lane"
        pids = dashboard.kill_targets(a["lane"], data["machine"], ctx.cfg)
        if not pids:
            return "nothing to stop (Docker, protected ports and Claude sessions are never stopped)"
        shown = " ".join(map(str, pids[:12])) + (" ..." if len(pids) > 12 else "")
        if ask(f"SIGTERM {len(pids)} processes of lane {a['lane']['name']}: {shown}?"):
            return acts.kill(pids)
        return ""
    return ""


def machine_alive(pid: int) -> bool:
    from slots import proc
    return proc.exists(pid)
