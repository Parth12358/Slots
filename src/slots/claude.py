"""Claude Code integration: hooks, settings install, and the generated skill.

Hooks run as `slots hook <event>` with the event's JSON on stdin:

  session-start    register the session (id, lane from cwd, its process) and tell it the rules
  user-prompt      the session is busy
  pre-tool-use     (Bash) heavy work needs an agent slot: block without one, or wrap it in a test slot
  stop             the session is idle
  notification     (idle_prompt) still idle: flags a held slot in the dashboard
  session-end      release everything the session holds

A hook never breaks a session: on any failure it logs to the state folder and
exits 0, letting the tool call through (a missing daemon must not stop work).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from slots import __version__, config, proc

HOOK_EVENTS = {   # settings event -> (our hook name, matcher)
    "SessionStart": ("session-start", None),
    "UserPromptSubmit": ("user-prompt", None),
    "PreToolUse": ("pre-tool-use", "Bash"),
    "Stop": ("stop", None),
    "Notification": ("notification", "idle_prompt"),
    "SessionEnd": ("session-end", None),
}
MARK = "# slots-hook"

CLAUDE_DEFAULTS = {
    # One-shot heavy commands: need an agent slot; in "wrap" mode they also run in a test slot.
    "heavy": [r"\bvitest\s+run\b", r"\bplaywright\s+test\b", r"\b(npm|pnpm|yarn|bun)\s+(run\s+)?(build|test)\b"],
    # Long-running local stacks: need an agent slot, never wrapped.
    "stack": [r"\bdocker[\s-]compose\s+up\b", r"\b(npm|pnpm|yarn|bun)\s+(run\s+)?(dev|start)\b", r"\bnext\s+dev\b"],
    # Already going through slots or the old scripts: never wrapped again.
    "exempt": [r"(^|[\s/;&|(])slots\s", r"\bslot\.sh\b", r"\bagent-slot\.sh\b", r"\brun_in_slot\b", r"\brun_exclusive\b"],
    "mode": "block",             # "block" or "wrap"
    "slot_pool": None,           # default: compat.agent_pool
    "wrap_pool": None,           # default: compat.test_pool
    "lanes_only": True,          # only enforce inside lane folders
    "notify": False,             # a desktop notification when a session idles holding a slot
}


def settings(cfg: config.Config) -> dict:
    s = {**CLAUDE_DEFAULTS, **cfg.raw.get("claude", {})}
    s["slot_pool"] = s["slot_pool"] or cfg.compat["agent_pool"]
    s["wrap_pool"] = s["wrap_pool"] or cfg.compat["test_pool"]
    if s["mode"] not in ("block", "wrap"):
        raise config.ConfigError('claude.mode must be "block" or "wrap"')
    for key in ("heavy", "stack", "exempt"):
        try:
            s[key + "_re"] = [re.compile(p) for p in s[key]]
        except re.error as e:
            raise config.ConfigError(f"claude.{key}: {e}") from None
    return s


def classify(command: str, s: dict) -> tuple[str | None, str | None]:
    """("heavy" | "stack" | None, the pattern that matched)."""
    for kind in ("heavy", "stack"):
        for rx in s[kind + "_re"]:
            if rx.search(command):
                return kind, rx.pattern
    return None, None


def exempt(command: str, s: dict) -> bool:
    return any(rx.search(command) for rx in s["exempt_re"])


# ---- the session's process -----------------------------------------------------------------------

CLAUDE_NAME = re.compile(r"^(claude|\d+\.\d+\.\d+)$")


def _parent_and_args(pid: int) -> tuple[int | None, str, str]:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=2).stdout.split(None, 1)
        args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, "", ""
    if not out:
        return None, "", ""
    return int(out[0]), (out[1].strip() if len(out) > 1 else ""), args


def is_claude(comm: str, args: str) -> bool:
    words = args.split()
    return bool(CLAUDE_NAME.match(os.path.basename(comm)) or (words and CLAUDE_NAME.match(os.path.basename(words[0])))
                or "@anthropic-ai/claude-code" in args
                or any(os.path.basename(w) == "claude" for w in words[:2]))


def session_pid(start: int | None = None, depth: int = 8) -> int | None:
    """The Claude Code process this hook runs under: the nearest ancestor that is a claude CLI."""
    forced = os.environ.get("SLOTS_SESSION_PID")
    if forced:
        return int(forced) if forced.isdigit() else None
    pid = start or os.getppid()
    for _ in range(depth):
        if not pid or pid <= 1:
            return None
        ppid, comm, args = _parent_and_args(pid)
        if is_claude(comm, args):
            return pid
        pid = ppid
    return None


# ---- the hooks -----------------------------------------------------------------------------------

class Hook:
    def __init__(self, ctx, payload: dict):
        from slots import cli
        self.ctx, self.cli, self.p = ctx, cli, payload
        self.sid = payload.get("session_id")
        self.cwd = payload.get("cwd") or os.getcwd()
        self.lane = ctx.cfg.lane_of(self.cwd)
        self.s = settings(ctx.cfg)

    def call(self, msg: dict, timeout: float = 5.0) -> dict | None:
        from slots.client import Client, Unavailable
        c = Client(self.ctx.paths, self.ctx.config_path)
        try:
            return c.call(msg, timeout)
        except (Unavailable, OSError, TimeoutError):
            return None
        finally:
            c.close()

    def register(self) -> dict | None:
        return self.call({"op": "session-start", "session": self.sid, "lane": self.lane, "cwd": self.cwd,
                          "pid": session_pid(), "source": self.p.get("source")})

    def view(self, state: str | None = None) -> dict | None:
        reply = self.call({"op": "session", "session": self.sid, "state": state})
        if reply and reply.get("session") is None:
            reply = self.register()   # the daemon lost track of it (or hooks were installed mid-session)
        return reply

    # each returns (stdout, exit code)

    def session_start(self) -> tuple[str, int]:
        reply = self.register()
        env_file = os.environ.get("CLAUDE_ENV_FILE")
        if env_file and self.sid:
            with open(env_file, "a") as f:
                f.write(f"export SLOTS_SESSION={shlex.quote(self.sid)}\n")
                if self.lane:
                    f.write(f"export SLOTS_LANE={shlex.quote(self.lane)}\n")
        if reply is None:
            return "", 0
        return self.context(self.summary(reply), "SessionStart"), 0

    def summary(self, reply: dict) -> str:
        pool = self.s["slot_pool"]
        used, size, waiting = reply["pools"].get(pool, [0, 0, 0])
        held = [l for l in reply["leases"] if l["resource"] == pool]
        if not self.lane:
            where = "This session isn't in a lane folder, so slots doesn't gate its commands."
        elif held:
            where = f"Lane {self.lane} holds {pool} slot {held[0]['unit']}."
        else:
            where = (f"Lane {self.lane} holds no {pool} slot ({used}/{size} in use"
                     + (f", {waiting} waiting" if waiting else "") + ").")
        return (f"slots: this machine's heavy local work is shared through slots. {where} Before starting a stack, "
                f"a dev server, a build or a full test suite: `slots acquire {pool} --wait`; run heavy commands as "
                f"`slots run {self.s['wrap_pool']} -- <command>`; release with `slots release {pool}` once your "
                "processes have stopped. Never touch another lane's slots. Rules: `slots help agents`.")

    @staticmethod
    def context(text: str, event: str) -> str:
        return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})

    def user_prompt(self) -> tuple[str, int]:
        self.view("busy")
        return "", 0

    def stop(self) -> tuple[str, int]:
        self.view("idle")
        return "", 0

    def notification(self) -> tuple[str, int]:
        if self.p.get("notification_type", "idle_prompt") != "idle_prompt":
            return "", 0
        reply = self.view("idle-prompt")
        pool = self.s["slot_pool"]
        if reply and self.s["notify"] and any(l["resource"] == pool for l in reply.get("leases", [])):
            notify("slots", f"Lane {self.lane or '?'} is idle and holds a {pool} slot")
        return "", 0

    def session_end(self) -> tuple[str, int]:
        reply = self.call({"op": "session-end", "session": self.sid, "reason": self.p.get("reason")}, timeout=1.0)
        if reply and reply.get("leases"):
            names = ", ".join(f"{l['resource']} slot {l['unit']}" if l.get("pool") else l["resource"]
                              for l in reply["leases"])
            print(f"slots: released {names}", file=sys.stderr)
        return "", 0

    def pre_tool_use(self) -> tuple[str, int]:
        if self.p.get("tool_name") != "Bash":
            return "", 0
        tool_input = self.p.get("tool_input") or {}
        command = tool_input.get("command") or ""
        kind, pattern = classify(command, self.s)
        reply = self.view()
        if kind is None or (self.s["lanes_only"] and not self.lane):
            return "", 0
        if reply is None:
            return "", 0   # fail open: no daemon, no gate (logged by the caller's stderr)
        pool = self.s["slot_pool"]
        if not any(l["resource"] == pool for l in reply.get("leases", [])):
            what = "a long-running local stack" if kind == "stack" else "heavy local work"
            reason = (f"slots: this looks like {what} (matched /{pattern}/), and lane {self.lane} holds no {pool} "
                      f"slot. First run `slots acquire {pool} --wait` (it waits in line), then run this again"
                      + (f" as `slots run {self.s['wrap_pool']} -- {command}`" if kind == "heavy" else "")
                      + f". Release it with `slots release {pool}` when your processes have stopped. "
                      "See `slots help agents`.")
            return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                      "permissionDecisionReason": reason}}), 0
        if kind == "heavy" and self.s["mode"] == "wrap" and not exempt(command, self.s):
            label = f"{self.lane} {command.split()[0] if command.split() else 'cmd'}"
            slots = self.ctx.slots_cmd
            wrapped = f"{slots} run {self.s['wrap_pool']} --label {shlex.quote(label)} -- bash -c {shlex.quote(command)}"
            return json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": {**tool_input, "command": wrapped},
                "additionalContext": f"slots ran this in a {self.s['wrap_pool']} slot (it waits for one if all are busy).",
            }}), 0
        return "", 0


HANDLERS = {"session-start": Hook.session_start, "user-prompt": Hook.user_prompt, "pre-tool-use": Hook.pre_tool_use,
            "stop": Hook.stop, "notification": Hook.notification, "session-end": Hook.session_end}


def run_hook(ctx, event: str, stdin_text: str) -> int:
    log = ctx.paths.home / "hooks.log"
    try:
        payload = json.loads(stdin_text or "{}")
        if event not in HANDLERS:
            raise ValueError(f"unknown hook {event!r}")
        out, code = HANDLERS[event](Hook(ctx, payload))
        if out:
            print(out)
        return code
    except Exception as e:   # never break the session
        try:
            ctx.paths.ensure()
            with open(log, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {event}: {type(e).__name__}: {e}\n")
        except OSError:
            pass
        return 0


def notify(title: str, text: str):
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", f"display notification {json.dumps(text)} with title {json.dumps(title)}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", title, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ---- installing into Claude Code's settings ------------------------------------------------------

def settings_path(scope: str, cwd: str | None = None) -> Path:
    if scope == "user":
        return Path("~/.claude/settings.json").expanduser()
    return Path(cwd or os.getcwd()) / ".claude" / "settings.local.json"


def ours(hook: dict) -> bool:
    return isinstance(hook, dict) and MARK in str(hook.get("command", ""))


def merged(current: dict, slots_cmd: str | None) -> dict:
    """`current` settings with our hooks replaced (or, with slots_cmd None, removed). Others are kept."""
    out = json.loads(json.dumps(current))
    hooks = out.get("hooks", {}) if isinstance(out.get("hooks"), dict) else {}
    for event, entries in list(hooks.items()):
        kept = []
        for entry in entries if isinstance(entries, list) else []:
            inner = [h for h in entry.get("hooks", []) if not ours(h)] if isinstance(entry, dict) else []
            if inner:
                kept.append({**entry, "hooks": inner})
            elif isinstance(entry, dict) and not entry.get("hooks"):
                kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if slots_cmd:
        for event, (name, matcher) in HOOK_EVENTS.items():
            entry = {"hooks": [{"type": "command", "command": f"{slots_cmd} hook {name}  {MARK}",
                                "timeout": 10 if name != "session-end" else 5}]}
            if matcher:
                entry = {"matcher": matcher, **entry}
            hooks.setdefault(event, []).append(entry)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def apply(path: Path, new: dict, backup_dir: Path, dry_run: bool) -> str:
    old_text = path.read_text() if path.exists() else ""
    new_text = json.dumps(new, indent=2) + "\n"
    diff = "".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                        str(path), str(path) + " (new)"))
    if dry_run or not diff:
        return diff or "no change"
    path.parent.mkdir(parents=True, exist_ok=True)
    if old_text:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{path.name}.{time.strftime('%Y%m%d%H%M%S')}.bak"
        backup.write_text(old_text)
    tmp = path.with_name(f".{path.name}.slots-tmp")
    tmp.write_text(new_text)
    os.replace(tmp, path)
    return diff


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise config.ConfigError(f"{path} isn't valid JSON ({e}); fix it first, nothing was changed") from None
    if not isinstance(data, dict):
        raise config.ConfigError(f"{path} isn't a JSON object; nothing was changed")
    return data


# ---- the generated skill -------------------------------------------------------------------------

def skill(cfg: config.Config) -> str:
    s = settings(cfg)
    pool, test = s["slot_pool"], s["wrap_pool"]
    res = cfg.resources
    rows = []
    for name, r in sorted(res.items(), key=lambda kv: (kv[1].kind != "pool", kv[0])):
        what = f"{r.size} slots" if r.kind == "pool" else "1 at a time"
        how = {"session": f"`slots acquire {name} --wait` ... `slots release {name}`",
               "command": f"`slots run {name} -- <command>`"}[r.hold]
        rows.append(f"| `{name}` | {r.kind} | {what} | {how} |")
    for name, f in sorted(cfg.families.items()):
        rows.append(f"| `{name}:<key>` | lock family | 1 per key | `slots run {name}:<key> -- <command>` |")
    root = f"`{cfg.lanes_root}`" if cfg.lanes_root else "the configured root"
    gate = ("blocks" if s["mode"] == "block" else "blocks, and wraps one-shot heavy commands in a "
            f"`{test}` slot,")
    return f"""---
name: local-slots
description: >-
  How sessions share this machine's heavy local work through slots: the {pool} slot to hold
  before starting a stack, dev server, build, full test suite or e2e run; `slots run` for heavy
  commands; the locks; and what to do when everything is taken. Use before running anything
  heavy locally, when a command prints "waiting for" or "not free", when a Bash call is blocked
  by slots, and before stopping, releasing or clearing anything.
---

# Local slots

Generated by `slots claude skill` from the slots config (slots {__version__}); the limits below are
the live ones. Rerun it after changing the config.

Every lane is a folder under {root}; your lane is the one your working folder is in. Slots are how
lanes take turns, so nobody crashes the machine or breaks another lane's run.

| Resource | Kind | Limit | How to hold it |
|---|---|---|---|
{chr(10).join(rows)}

## The rules

1. **Hold a `{pool}` slot before you start anything that runs locally**: a stack, a dev server, a
   build, a full suite, an e2e run: `slots acquire {pool} --wait`. Reading and editing code and
   running one small test file need no slot. A hook {gate} heavy commands without one.
2. **Run heavy commands in a `{test}` slot**: `slots run {test} -- <command>`. It waits for a free
   slot, exits with the command's exit code, and frees the slot when the command ends.
3. **Locks go around the run only**, e.g. `slots run {test} e2e -- <e2e command>` if `e2e` exists.
4. **Release as soon as your processes have stopped**: `slots release {pool}`. Don't hold a slot
   while waiting for CI, a review, a person or another agent.
5. **When everything is taken, wait** (`--wait`); never work around a limit or change one.
6. **Never touch another lane's slot, lock, processes or ports.** If something looks stuck, report
   it with `slots status`; don't clear it.
7. **When you're paused, stopped or done**, stop your processes and `slots release --all`. (Ending
   the session releases everything it holds too.)

## Commands

```bash
slots acquire {pool} --wait          # before your stack
slots run {test} -- npx vitest run   # a heavy command, in a test slot
slots release {pool}                 # after your processes have stopped
slots status                         # who holds and awaits what
slots help agents                    # these rules, in full
```

## When something's wrong

| You see | Do |
|---|---|
| `waiting for {pool} (... in use: ...); #N in line` | Wait; you start by yourself. |
| `{pool} not free: ...` (exit 1) | Run again with `--wait`. |
| A Bash call blocked: "holds no {pool} slot" | `slots acquire {pool} --wait`, then run it again. |
| `refused: ... held by ...` | It isn't yours. Leave it. |
| `can't reach the slots daemon` | Report it with the error; don't work around it. |

More: `slots help` (offline), or https://parth12358.github.io/Slots/wiki/agents.html.
"""


def skill_path(scope: str, cwd: str | None = None) -> Path:
    base = Path("~/.claude").expanduser() if scope == "user" else Path(cwd or os.getcwd()) / ".claude"
    return base / "skills" / "local-slots" / "SKILL.md"
