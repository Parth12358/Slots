"""The manual: `slots help [topic]`, `slots man [--install]`, and the wiki pages on GitHub Pages.

One source, the Markdown pages in slots/manual/, shipped inside the package so
they work offline. `python -m slots.manual site <dir>` writes the wiki for the
Pages build (one page per topic, plus all.txt and llms.txt for agents).
"""
from __future__ import annotations

import re
import sys
import time
from importlib import resources
from pathlib import Path

from slots import __version__

# Reading order, for the man page and all.txt.
TOPICS = ["quickstart", "setup", "agents", "concepts", "claude", "cli", "config", "dashboard", "compat", "troubleshooting"]
SITE = "https://parth12358.github.io/Slots"


def page(name: str) -> str:
    return resources.files("slots").joinpath("manual", f"{name}.md").read_text()


def topics() -> list[tuple[str, str]]:
    """(topic, one-line description) from the index's table."""
    return re.findall(r"^\| \[(\w+)\]\(\w+\.md\) \| (.+?) \|$", page("index"), re.M)


def command_section(cmd: str) -> str | None:
    """The `## slots <cmd>` section of the cli page."""
    text = page("cli")
    m = re.search(rf"^## slots {re.escape(cmd)}\b.*?(?=^## |\Z)", text, re.M | re.S)
    return m.group(0).rstrip() if m else None


def help_text(topic: str | None) -> tuple[int, str]:
    if not topic:
        rows = "\n".join(f"  {t:<16} {d}" for t, d in topics())
        return 0, (f"slots {__version__}: share one machine's heavy work between many sessions.\n\n"
                   f"slots help <topic>, one of:\n{rows}\n\n"
                   "slots help <command>   (run, acquire, release, status, dash, ...)\n"
                   "slots man [--install]  the whole manual as a man page\n"
                   f"on the web: {SITE}/wiki/   (all of it, for agents: {SITE}/wiki/all.txt)")
    if topic in TOPICS or topic == "index":
        return 0, page(topic).rstrip()
    section = command_section(topic)
    if section:
        return 0, section
    return 2, f"slots: no help topic {topic!r}; try: {', '.join(TOPICS)}, or a command name"


# ---- man page ------------------------------------------------------------------------------------

def _roff_escape(s: str) -> str:
    s = s.replace("\\", "\\e").replace("-", "\\-")
    s = "".join(c if ord(c) < 128 else f"\\[u{ord(c):04X}]" for c in s)
    return "\\&" + s if s.startswith((".", "'")) else s


def _inline(s: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\((?:\w+\.md|[^)]*)\)", r"\1", s)        # links: keep the text
    out, parts = [], re.split(r"(`[^`]+`|\*\*[^*]+\*\*)", s)
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            out.append("\\fB" + _roff_escape(p[1:-1]).removeprefix("\\&") + "\\fR")
        elif p.startswith("**") and p.endswith("**") and len(p) > 3:
            out.append("\\fB" + _roff_escape(p[2:-2]).removeprefix("\\&") + "\\fR")
        else:
            out.append(_roff_escape(p).removeprefix("\\&"))
    text = "".join(out)
    return "\\&" + text if text.startswith((".", "'")) else text


def to_roff(md: str, section_title: str | None = None) -> list[str]:
    """One manual page's Markdown as man(7) lines: # -> .SH, ## -> .SS, code -> .nf, tables -> .TP."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            i += 1
            out += [".PP", ".RS 4", ".nf"]
            while i < len(lines) and not lines[i].startswith("```"):
                out.append(_roff_escape(lines[i]) if lines[i] else "")
                i += 1
            out += [".fi", ".RE"]
        elif line.startswith("# "):
            out.append(f'.SH "{(section_title or line[2:]).upper()}"')
        elif line.startswith("## "):
            out.append(f'.SS "{_inline(line[3:])}"')
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", lines[i])[1:-1]]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            for cells in rows[1:]:          # the first row is the header
                out += [".TP", _inline(cells[0]), _inline(" — ".join(c for c in cells[1:] if c))]
            continue
        elif re.match(r"^(\d+\.|-) ", line):
            bullet = "\\(bu" if line.startswith("-") else line.split()[0]
            text = [re.sub(r"^(\d+\.|-) ", "", line)]
            while i + 1 < len(lines) and lines[i + 1].startswith("   ") and lines[i + 1].strip():
                i += 1
                text.append(lines[i].strip())
            out += [f".IP {bullet} 4", _inline(" ".join(text))]
        elif not line.strip():
            if out and out[-1] != ".PP":
                out.append(".PP")
        else:
            out.append(_inline(line))
        i += 1
    return out


def man_page() -> str:
    intro = page("index").split("\n| Topic", 1)[0]
    intro = re.sub(r"^# .*\n", "", intro).strip()
    lines = [f'.TH SLOTS 1 "{time.strftime("%Y-%m-%d")}" "slots {__version__}" "slots manual"',
             ".SH NAME", "slots \\- share one machine's heavy work between many sessions",
             ".SH SYNOPSIS", ".B slots", "[\\fIcommand\\fR] [\\fIoptions\\fR]",
             ".SH DESCRIPTION", *to_roff(intro)]
    for t in TOPICS:
        lines += to_roff(page(t))
    lines += [".SH SEE ALSO", f"{SITE}/wiki/ and \\fBslots help\\fR <topic>."]
    return "\n".join(tidy(lines)) + "\n"


BLOCK = (".SH", ".SS", ".PP", ".TP", ".IP", ".RS")


def tidy(lines: list[str]) -> list[str]:
    """What strict checkers (mandoc -W warning) want: no paragraph macro right after a heading or
    before another block, no blank text lines outside no-fill blocks, no trailing whitespace."""
    out: list[str] = []
    nofill = False
    for i, line in enumerate(lines):
        line = line.rstrip()
        nxt = next((l for l in lines[i + 1:] if l.strip()), "")
        if line == ".nf":
            nofill = True
        elif line == ".fi":
            nofill = False
        if line == ".PP" and (not out or out[-1].startswith((".SH", ".SS", ".PP", ".TH"))
                              or nxt.startswith(BLOCK) or not nxt):
            continue
        if not line and not nofill:
            continue
        out.append(line)
    return out


def install_man(base: Path | None = None) -> Path:
    base = base or Path("~/.local/share/man").expanduser()
    target = base / "man1" / "slots.1"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(man_page())
    return target


# ---- the wiki ------------------------------------------------------------------------------------

def setup_prompt() -> str:
    """The copy-and-paste prompt from the setup page."""
    return re.search(r"```text\n(.*?)\n```", page("setup"), re.S).group(1)


def title(t: str) -> str:
    return re.match(r"# (.+)", page(t)).group(1)


def site(out: Path):
    """Write the wiki for GitHub Pages into `out` (docs/wiki), plus the site's data next to it:
    _data/topics.yml (the sidebar), _includes/setup_prompt.txt (the home page's prompt), llms.txt."""
    out.mkdir(parents=True, exist_ok=True)
    docs = out.parent
    blurbs = dict(topics())
    order = ["index", *TOPICS]
    for i, t in enumerate(order):
        prev_t = order[i - 1] if i > 0 else None
        next_t = order[i + 1] if i + 1 < len(order) else None
        meta = [f'title: "{title(t)}"', "layout: wiki", f"slug: {t}"]
        if prev_t:
            meta += [f"prev: {prev_t}", f'prev_title: "{title(prev_t)}"']
        if next_t:
            meta += [f"next: {next_t}", f'next_title: "{title(next_t)}"']
        (out / f"{t}.md").write_text("---\n" + "\n".join(meta) + "\n---\n\n" + page(t).rstrip() + "\n")
    (docs / "_data").mkdir(exist_ok=True)
    (docs / "_data" / "topics.yml").write_text("".join(
        f'- slug: {t}\n  title: "{title(t)}"\n  blurb: "{blurbs.get(t, "").replace(chr(34), chr(39))}"\n'
        for t in order))
    (docs / "_includes").mkdir(exist_ok=True)
    (docs / "_includes" / "setup_prompt.txt").write_text(setup_prompt() + "\n")
    all_text = "\n\n".join(page(t).rstrip() for t in order)
    (out / "all.txt").write_text(f"# slots {__version__}: the whole manual\n# {SITE}/wiki/\n\n{all_text}\n")
    (docs / "llms.txt").write_text(
        f"# slots\n\n> Share one machine's heavy work between many Claude Code sessions: pools, locks, leases, a FIFO queue.\n\n"
        f"Agents: read {SITE}/wiki/agents.html first; the whole manual as plain text is {SITE}/wiki/all.txt. "
        f"To set slots up on a machine, follow {SITE}/wiki/setup.html.\n\n"
        "## Pages\n\n" + "\n".join(f"- [{t}]({SITE}/wiki/{t}.html): {d}" for t, d in topics()) + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "site":
        site(Path(sys.argv[2]))
    else:
        print("usage: python -m slots.manual site <dir>", file=sys.stderr)
        sys.exit(2)
