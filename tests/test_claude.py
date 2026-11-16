"""Claude Code integration: hooks, sessions, settings install, the skill."""
import json
import os
import stat
import subprocess
import sys
import time
import tomllib
import unittest
from pathlib import Path

from slots import claude, config, dashboard
from tests.helpers import World


class ClaudeWorld(World):
    def setUp(self):
        super().setUp()
        # A stand-in for the Claude Code process the hooks run under.
        self.claude = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(self.claude)

    def hook(self, event, payload, *, env=None):
        e = {**self.env, "SLOTS_SESSION_PID": str(self.claude.pid), **(env or {})}
        r = subprocess.run([sys.executable, "-m", "slots", "hook", event], input=json.dumps(payload), env=e,
                           capture_output=True, text=True, timeout=30, cwd=self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None, r

    def start(self, sid="s1", lane="301", **kw):
        self.lane(lane)
        return self.hook("session-start", {"session_id": sid, "cwd": str(self.root / lane), "source": "startup"}, **kw)

    def bash(self, command, sid="s1", lane="301", **extra):
        return self.hook("pre-tool-use", {"session_id": sid, "cwd": str(self.root / lane), "tool_name": "Bash",
                                          "tool_input": {"command": command, "description": "x", **extra}})[0]

    def sessions(self):
        return self.state()["sessions"]


class HookTest(ClaudeWorld):
    def test_session_start_registers_and_sets_the_environment(self):
        env_file = self.dir / "env"
        out, _ = self.start(env={"CLAUDE_ENV_FILE": str(env_file)})
        ctx = out["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertIn("Lane 301 holds no agent slot (0/2 in use)", ctx["additionalContext"])
        self.assertIn("slots acquire agent --wait", ctx["additionalContext"])
        self.assertEqual(env_file.read_text(), "export SLOTS_SESSION=s1\nexport SLOTS_LANE=301\n")
        (s,) = self.sessions()
        self.assertEqual((s["id"], s["lane"], s["pid"], s["state"]), ("s1", "301", self.claude.pid, "idle"))

    def test_outside_a_lane(self):
        out, _ = self.hook("session-start", {"session_id": "s9", "cwd": str(self.dir)})
        self.assertIn("isn't in a lane folder", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook("pre-tool-use", {"session_id": "s9", "cwd": str(self.dir), "tool_name": "Bash",
                                                     "tool_input": {"command": "npx vitest run"}})[0])

    def test_heavy_without_a_slot_is_blocked_with_how_to(self):
        self.start()
        out = self.bash("cd web && npx vitest run --reporter dot")
        o = out["hookSpecificOutput"]
        self.assertEqual((o["hookEventName"], o["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn("lane 301 holds no agent slot", o["permissionDecisionReason"])
        self.assertIn("slots acquire agent --wait", o["permissionDecisionReason"])
        self.assertIn("slots run test -- cd web && npx vitest run", o["permissionDecisionReason"])
        o = self.bash("docker compose up -d")["hookSpecificOutput"]
        self.assertIn("long-running local stack", o["permissionDecisionReason"])
        self.assertNotIn("slots run test", o["permissionDecisionReason"], "stacks aren't wrapped in a test slot")

    def test_light_commands_and_other_tools_pass(self):
        self.start()
        for cmd in ("git status", "npx vitest run --help" if False else "ls -la", "cat package.json",
                    "slots acquire agent --wait", "slots status"):
            self.assertIsNone(self.bash(cmd), cmd)
        out, _ = self.hook("pre-tool-use", {"session_id": "s1", "cwd": str(self.root / "301"), "tool_name": "Read",
                                            "tool_input": {"file_path": "x"}})
        self.assertIsNone(out)

    def test_with_a_slot_heavy_runs(self):
        self.start()
        r = self.slots("acquire", "agent", lane="301", env={"SLOTS_SESSION": "s1"}, check=0)
        self.assertIn("took agent slot 1", r.stdout)
        h = self.resource("agent")["holders"][0]
        self.assertEqual((h["kind"], h["owner"]["pid"], h["owner"]["session"]), ("pid", self.claude.pid, "s1"),
                         "a slot taken in a session is tied to the session's process")
        self.assertIsNone(self.bash("npx vitest run"))

    def test_a_slot_taken_by_hand_for_the_lane_counts(self):
        self.start()
        self.slots("acquire", "agent", lane="301", check=0)   # no session
        self.assertIsNone(self.bash("npm run build"))

    def test_another_sessions_slot_does_not_count(self):
        self.start("s1")
        self.start("s2")
        self.slots("acquire", "agent", lane="301", env={"SLOTS_SESSION": "s2"}, check=0)
        self.assertEqual(self.bash("npm run build", sid="s1")["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_session_end_releases_everything(self):
        self.start()
        self.slots("acquire", "agent", lane="301", env={"SLOTS_SESSION": "s1"}, check=0)
        self.slots("acquire", "e2e", lane="301", env={"SLOTS_SESSION": "s1"}, check=0)
        _, r = self.hook("session-end", {"session_id": "s1", "reason": "prompt_input_exit"})
        self.assertIn("slots: released agent slot 1, e2e", r.stderr)
        self.assertEqual((self.resource("agent")["in_use"], self.resource("e2e")["in_use"]), (0, 0))
        self.assertEqual(self.sessions(), [])

    def test_session_process_death_releases_everything(self):
        self.start()
        self.slots("acquire", "agent", lane="301", env={"SLOTS_SESSION": "s1"}, check=0)
        os.killpg(self.claude.pid, 9)
        self.claude.wait()
        self.until(lambda: self.resource("agent")["in_use"] == 0 and not self.sessions(), timeout=8,
                   what="sweep to end the session")

    def test_busy_idle_and_unknown_sessions(self):
        self.start()
        self.hook("user-prompt", {"session_id": "s1", "cwd": str(self.root / "301"), "prompt": "go"})
        self.assertEqual(self.sessions()[0]["state"], "busy")
        self.hook("stop", {"session_id": "s1", "cwd": str(self.root / "301")})
        self.assertEqual(self.sessions()[0]["state"], "idle")
        self.hook("notification", {"session_id": "s1", "cwd": str(self.root / "301"), "notification_type": "idle_prompt"})
        self.assertEqual(self.sessions()[0]["idle_prompts"], 1)
        # A session the daemon never saw (hooks installed mid-session) is registered on first contact.
        self.hook("user-prompt", {"session_id": "late", "cwd": str(self.root / "302"), "prompt": "x"})
        self.lane("302")
        self.assertIn("late", [s["id"] for s in self.sessions()])

    def test_a_broken_hook_never_breaks_the_session(self):
        r = subprocess.run([sys.executable, "-m", "slots", "hook", "pre-tool-use"], input="not json", env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertIn("pre-tool-use", (self.home / "hooks.log").read_text())
        r = subprocess.run([sys.executable, "-m", "slots", "--config", "/nope.toml", "hook", "session-start"],
                           input="{}", env={**self.env, "SLOTS_HOME": str(self.home)}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)


class WrapTest(ClaudeWorld):
    extra_config = """
    [claude]
    mode = "wrap"
    """

    def test_wrap_mode_runs_heavy_commands_in_a_test_slot(self):
        self.start()
        self.slots("acquire", "agent", lane="301", env={"SLOTS_SESSION": "s1"}, check=0)
        o = self.bash("npx vitest run 'a b'", run_in_background=False)["hookSpecificOutput"]
        new = o["updatedInput"]
        self.assertEqual((new["description"], new["run_in_background"]), ("x", False), "other fields kept")
        self.assertIn(" run test --label '301 npx' -- bash -c ", new["command"])
        r = subprocess.run(new["command"].replace("npx vitest run", "echo"), shell=True, env=self.env,
                           capture_output=True, text=True, cwd=self.dir, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.bash("slots run test -- npx vitest run"), "already wrapped")
        self.assertIsNone(self.bash("docker compose up"), "stacks aren't wrapped")
        # Without the agent slot it's still blocked.
        self.start("s2", lane="302")
        self.assertEqual(self.bash("npx vitest run", sid="s2", lane="302")["hookSpecificOutput"]["permissionDecision"],
                         "deny")


class InstallTest(World):
    def test_install_dry_run_idempotent_and_uninstall(self):
        home = self.dir / "home"
        settings = home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        mine = {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command",
                                                                                         "command": "my-linter"}]}]}}
        settings.write_text(json.dumps(mine))
        env = {"HOME": str(home)}
        r = self.slots("claude", "install", "--dry-run", env=env, check=0)
        self.assertIn("(dry run: nothing was written)", r.stdout)
        self.assertIn('+        "matcher": "Bash"', r.stdout)
        self.assertEqual(json.loads(settings.read_text()), mine)

        self.slots("claude", "install", env=env, check=0)
        new = json.loads(settings.read_text())
        self.assertEqual(new["model"], "opus")
        self.assertEqual(set(new["hooks"]), set(claude.HOOK_EVENTS))
        pre = new["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], mine["hooks"]["PreToolUse"][0], "other hooks are kept as they were")
        self.assertEqual(pre[1]["matcher"], "Bash")
        cmd = pre[1]["hooks"][0]["command"]
        self.assertIn("hook pre-tool-use", cmd)
        self.assertEqual(new["hooks"]["Notification"][0]["matcher"], "idle_prompt")
        self.assertEqual(list((self.home / "backups").iterdir())[0].read_text(), json.dumps(mine))

        r = self.slots("claude", "install", env=env, check=0)
        self.assertIn("no change", r.stdout)

        # The installed command really runs the hook.
        out = subprocess.run(cmd, shell=True, input="{}", env={**self.env, "HOME": str(home)}, capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0)

        self.slots("claude", "uninstall", env=env, check=0)
        self.assertEqual(json.loads(settings.read_text()), mine)

    def test_project_scope_and_bad_json(self):
        proj = self.dir / "proj"
        proj.mkdir()
        r = subprocess.run([sys.executable, "-m", "slots", "claude", "install", "--project"], cwd=proj, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SessionStart", json.loads((proj / ".claude" / "settings.local.json").read_text())["hooks"])
        (proj / ".claude" / "settings.local.json").write_text("{oops")
        r = subprocess.run([sys.executable, "-m", "slots", "claude", "install", "--project"], cwd=proj, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("isn't valid JSON", r.stderr)
        self.assertEqual((proj / ".claude" / "settings.local.json").read_text(), "{oops")


class SkillTest(World):
    agent_size = 7

    def test_skill_is_generated_from_the_config(self):
        r = self.slots("claude", "skill", "--print", check=0)
        text = r.stdout
        self.assertTrue(text.startswith("---\nname: local-slots\ndescription: >-\n"))
        self.assertIn("| `agent` | pool | 7 slots | `slots acquire agent --wait` ... `slots release agent` |", text)
        self.assertIn("| `test` | pool | 2 slots | `slots run test -- <command>` |", text)
        self.assertIn("| `next-employee:<key>` | lock family |", text)
        self.assertIn(str(self.root), text)
        out = self.dir / "SKILL.md"
        self.assertIn("wrote", self.slots("claude", "skill", "-o", str(out), check=0).stdout)
        self.assertIn("unchanged", self.slots("claude", "skill", "-o", str(out), check=0).stdout)
        self.config.write_text(self.config.read_text().replace("size = 7", "size = 9"))
        self.slots("claude", "skill", "-o", str(out), check=0)
        self.assertIn("| `agent` | pool | 9 slots |", out.read_text())


class SessionDashboardTest(unittest.TestCase):
    def test_idle_session_holding_a_slot_needs_you(self):
        cfg = config.from_dict(tomllib.loads('[lanes]\nroot="/l"\npattern=\'^(?P<ticket>\\d+)$\'\n'
                                             '[pool.agent]\nsize=2\nhold="session"\n'))
        T = 1_000_000.0
        h = {"id": "L", "resource": "agent", "unit": 1, "kind": "pid", "since": T, "last_active": T,
             "owner": {"lane": "1", "session": "abcdef123", "pid": 50}}
        sess = {"id": "abcdef123", "lane": "1", "cwd": "/l/1", "pid": 50, "pid_start": None, "started": T,
                "last_active": T, "state": "idle", "state_since": T + 60, "source": "startup", "idle_prompts": 2,
                "leases": ["L"]}
        state = {"now": T, "events": [], "sessions": [sess], "resources": [
            {"name": "agent", "kind": "pool", "size": 2, "hold": "session", "in_use": 1, "holders": [h], "queue": []}]}
        procs = {50: {"pid": 50, "ppid": 1, "cpu": 1, "rss": 1, "etime": "1", "secs": 1, "cmd": "claude"},
                 51: {"pid": 51, "ppid": 50, "cpu": 1, "rss": 1, "etime": "1", "secs": 1, "cmd": "node mcp-server.js"}}
        mach = {"procs": procs, "ports": [], "containers": [], "load": None, "cores": 1, "cwds": {}}
        model = dashboard.build(state, mach, cfg, now=T + 60 + 31 * 60)
        self.assertEqual(model["sessions"][0]["pids"], [51])
        self.assertEqual(model["lanes"][0]["pids"], [], "the session's own tree isn't lane work")
        self.assertIn("idle? session abcdef12 (1) is idle and holds agent slot 1", model["needs"][0]["text"])
        sess["state"] = "busy"
        self.assertEqual(dashboard.build(state, mach, cfg, now=T + 60 + 31 * 60)["needs"], [])
        self.assertIn("SESSIONS  1", dashboard.text(model))


class ClassifyTest(unittest.TestCase):
    def test_defaults(self):
        s = claude.settings(config.from_dict({}))
        cases = {"npx vitest run": "heavy", "pnpm vitest run src": "heavy", "npx playwright test --project=x": "heavy",
                 "npm run build": "heavy", "npm test": "heavy", "yarn build": "heavy",
                 "docker compose up -d": "stack", "docker-compose up": "stack", "npm run dev": "stack",
                 "pnpm start": "stack", "next dev": "stack",
                 "git status": None, "npx vitest": None, "vim build.ts": None, "npm install": None}
        for cmd, want in cases.items():
            self.assertEqual(claude.classify(cmd, s)[0], want, cmd)
        self.assertTrue(claude.exempt("slots run test -- npm test", s))
        self.assertTrue(claude.exempt("~/x/slot.sh --label a -- npm test", s))
        self.assertFalse(claude.exempt("npm test", s))

    def test_is_claude(self):
        self.assertTrue(claude.is_claude("claude", "claude --resume"))
        self.assertTrue(claude.is_claude("2.1.3", "2.1.3"))
        self.assertTrue(claude.is_claude("node", "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"))
        self.assertFalse(claude.is_claude("zsh", "zsh -c ls"))
        self.assertFalse(claude.is_claude("node", "node server.js"))

    def test_finds_the_claude_ancestor(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        fake = d / "claude"
        # claude -> sh -c (the hook's shell) -> python: the walk passes the shell and stops at claude.
        fake.write_text(f"#!/bin/sh\nsh -c '{sys.executable} -c \"from slots import claude; "
                        "print(claude.session_pid())\"; echo $PPID'\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        env = {k: v for k, v in os.environ.items() if k != "SLOTS_SESSION_PID"}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
        out = subprocess.run([str(fake)], capture_output=True, text=True, env=env, timeout=30).stdout.split()
        self.assertEqual(out[0], out[1], "the session pid is the `claude` process the hook runs under")


if __name__ == "__main__":
    unittest.main()
