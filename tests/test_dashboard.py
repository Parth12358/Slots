"""The dashboard: its model, config edits, actions through the broker, and the curses view in a pty."""
import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
import tomllib
import unittest
from pathlib import Path

from slots import config, config_edit, dashboard
from tests.helpers import World

T0 = 1_000_000.0


def cfg(extra: str = "") -> config.Config:
    return config.from_dict(tomllib.loads("""
[lanes]
root = "/lanes"
pattern = '^(?P<ticket>\\d+)$'
[pool.agent]
size = 2
hold = "session"
[lock.e2e]
[protect]
ports = [5432]
""" + extra))


def lease(res, unit, lane, kind="manual", pid=None, since=T0, last_active=None, lid="x"):
    return {"id": lid, "resource": res, "unit": unit, "kind": kind, "since": since,
            "last_active": last_active or since, "owner": {"lane": lane, "label": lane, "pid": pid}}


def state(holders=(), queue=(), size=2):
    return {"now": T0, "events": [], "resources": [
        {"name": "agent", "kind": "pool", "size": size, "hold": "session", "in_use": len(holders),
         "holders": list(holders), "queue": list(queue)},
        {"name": "e2e", "kind": "lock", "size": 1, "hold": "command", "in_use": 0, "holders": [], "queue": []}]}


def proc(pid, ppid, cmd, cpu=1.0, rss=1024):
    return {"pid": pid, "ppid": ppid, "cpu": cpu, "rss": rss, "etime": "01:00", "secs": 60, "cmd": cmd}


def mach(procs=(), ports=(), **kw):
    return {"procs": {p["pid"]: p for p in procs}, "ports": list(ports), "containers": [], "load": [1, 1, 1],
            "cores": 8, "mem_free": 8.0, "mem_total": 16.0, "swap_used": 0.0, "swap_total": 4.0, "cwds": {}, **kw}


class ModelTest(unittest.TestCase):
    def test_exact_attribution_follows_the_holders_process_tree(self):
        s = state([lease("agent", 1, "10526", kind="pid", pid=100)])
        m = mach([proc(100, 1, "bash"), proc(101, 100, "node server.js", cpu=50), proc(102, 101, "esbuild"),
                  proc(200, 1, "node other")], ports=[{"cmd": "node", "pid": 101, "port": 4352}])
        model = dashboard.build(s, m, cfg(), now=T0 + 60)
        ln = model["lanes"][0]
        self.assertEqual((ln["name"], ln["pids"], ln["ports"]), ("10526", [100, 101, 102], [4352]))
        self.assertEqual(ln["fallback"], set())

    def test_fallback_by_command_line_and_folder(self):
        m = mach([proc(300, 1, "node /lanes/11852/node_modules/.bin/vite"), proc(301, 1, "node server.js"),
                  proc(302, 1, "vim /lanes/11852/a.ts"), proc(303, 1, "node /lanes/ta-e2e/x.js")], cwds={301: "11860"})
        model = dashboard.build(state(), m, cfg(), now=T0)
        lanes = {l["name"]: l for l in model["lanes"]}
        self.assertEqual(lanes["11852"]["pids"], [300], "vim doesn't match the process pattern")
        self.assertEqual(lanes["11860"]["fallback"], {301}, "found by working folder")
        self.assertNotIn("ta-e2e", lanes, "folders that aren't lanes are ignored")

    def test_idle_needs_you(self):
        s = state([lease("agent", 1, "10526"), lease("agent", 2, "11852", lid="y")])
        m = mach([proc(300, 1, "node /lanes/11852/x.js")])
        needs = dashboard.build(s, m, cfg(), now=T0 + 31 * 60)["needs"]
        self.assertEqual(len(needs), 1)
        self.assertIn("idle? 10526 holds agent slot 1 with no processes for 31m; no pid ties it", needs[0]["text"])
        self.assertEqual(needs[0]["fix"]["lease"]["id"], "x")
        self.assertEqual(dashboard.build(s, m, cfg(), now=T0 + 29 * 60)["needs"], [])

    def test_activity_resets_idle(self):
        s = state([lease("agent", 1, "1", last_active=T0 + 25 * 60)])
        self.assertEqual(dashboard.build(s, mach(), cfg(), now=T0 + 40 * 60)["needs"], [])

    def test_long_held_and_long_wait_and_thrashing(self):
        s = state([lease("agent", 1, "1", kind="pid", pid=100)],
                  queue=[{"token": "t", "seq": 1, "owner": {"lane": "2"}, "resources": ["agent"], "since": T0,
                          "waited": 2 * 3600, "deadline": None, "orphan": False}])
        m = mach([proc(100, 1, "bash")], mem_free=1.0)
        needs = dashboard.build(s, m, cfg(), now=T0 + 13 * 3600)["needs"]
        texts = [n["text"] for n in needs]
        self.assertTrue(texts[0].startswith("machine is thrashing"), texts)
        self.assertIn("long-held: 1 has held agent slot 1 for 13h00m", texts)
        self.assertIn("2 has waited 2h00m for agent", texts)
        self.assertEqual(next(n for n in needs if "waited" in n["text"])["fix_label"], "move to front")

    def test_over_limit_and_config_mismatch(self):
        s = state([lease("agent", u, str(u), kind="pid", pid=100, lid=str(u)) for u in (1, 2, 3)], size=2)
        s["daemon"] = {"config": "/elsewhere.toml"}
        needs = [n["text"] for n in dashboard.build(s, mach([proc(100, 1, "x")]), cfg(), now=T0,
                                                     config_path="/mine.toml")["needs"]]
        self.assertTrue(any("over its limit (3/2)" in t for t in needs))
        self.assertTrue(any("the daemon uses /elsewhere.toml" in t for t in needs))

    def test_kill_targets_spare_docker_claude_and_protected_ports(self):
        ln = {"pids": [1, 2, 3, 4, 5]}
        m = mach([proc(1, 0, "node a.js"), proc(2, 0, "/Applications/Docker.app/com.docker.backend"),
                  proc(3, 0, "claude --resume"), proc(4, 0, "postgres -D x"), proc(5, 0, "vitest run")],
                 ports=[{"cmd": "postgres", "pid": 4, "port": 5432}])
        self.assertEqual(dashboard.kill_targets(ln, m, cfg()), [1, 5])

    def test_text(self):
        s = state([lease("agent", 1, "10526", kind="pid", pid=100)])
        s["events"] = [{"ts": T0, "kind": "granted", "lane": "10526", "label": "10526", "resources": ["agent#1"],
                        "waited": 0}]
        out = dashboard.text(dashboard.build(s, mach([proc(100, 1, "bash")]), cfg(), now=T0 + 60), actions=True)
        self.assertIn("LANES  1", out)
        self.assertIn("AGENT  pool  1/2 in use", out)
        self.assertIn("10526 took agent#1", out)
        self.assertIn("MACHINE", out)
        self.assertRegex(out, r"\* +#1 10526")


class ConfigEditTest(unittest.TestCase):
    TEXT = """# my config
[pool.agent]   # agents
size = 7       # don't go above 9
hold = "session"

[pool.test]
size = 5

[device.desktop]
match = "big*"
pool.agent.size = 12
"""

    def setUp(self):
        import tempfile
        self.d = Path(tempfile.mkdtemp())
        self.path = self.d / "c.toml"
        self.path.write_text(self.TEXT)

    def test_edits_only_the_size_line(self):
        self.assertEqual(config_edit.set_pool_size(self.path, "agent", 8), "[pool.agent] size: 7 -> 8")
        new = self.path.read_text()
        self.assertEqual(new, self.TEXT.replace("size = 7       #", "size = 8       #"))
        self.assertEqual(config_edit.set_pool_size(self.path, "test", 4), "[pool.test] size: 5 -> 4")
        self.assertIn("[pool.test]\nsize = 4\n", self.path.read_text())

    def test_device_override_is_edited_when_present(self):
        self.assertEqual(config_edit.set_pool_size(self.path, "agent", 10, device="desktop"),
                         "[device.desktop] pool.agent.size: 12 -> 10")
        self.assertIn("size = 7 ", self.path.read_text())
        self.assertEqual(config_edit.set_pool_size(self.path, "test", 3, device="desktop"), "[pool.test] size: 5 -> 3")

    def test_refusals(self):
        with self.assertRaises(config.ConfigError):
            config_edit.set_pool_size(self.path, "nope", 3)
        with self.assertRaises(config.ConfigError):
            config_edit.set_pool_size(self.path, "agent", 0)
        self.assertEqual(self.path.read_text(), self.TEXT)


def drive(argv, env, keys, rows=45, cols=160, settle=2.0, gap=0.7) -> tuple[int, str]:
    """Run a curses program in a pty of the given size, type `keys`, and return (exit code, plain output)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(argv[0], argv, env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    out = b""

    def pump(t):
        nonlocal out
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return
    pump(settle)
    for k in keys:
        os.write(fd, k.encode())
        pump(gap)
    deadline = time.time() + 10
    while time.time() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            os.close(fd)
            return os.waitstatus_to_exitcode(status), re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-B]|\x1b[=>]", "",
                                                             out.decode(errors="replace"))
        pump(0.1)
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    raise AssertionError("dashboard didn't exit")


class LiveTest(World):
    def dash(self, *args, keys=("q",), **kw):
        env = {**self.env, "TERM": "xterm-256color"}
        return drive([sys.executable, "-m", "slots", "dash", *args], env, list(keys), **kw)

    def test_once_without_a_terminal(self):
        self.slots("acquire", "agent", lane="10526", check=0)
        r = self.slots("dash", "--once", check=0)
        self.assertIn("AGENT  pool  1/2 in use", r.stdout)
        self.assertIn("LANES", r.stdout)
        self.assertIn("MACHINE", r.stdout)

    def test_dash_refuses_without_a_terminal(self):
        self.slots("dash", check=2)

    def test_status_full_json(self):
        self.slots("acquire", "agent", lane="10526", check=0)
        state = json.loads(self.slots("status", "--json", "--full", check=0).stdout)
        self.assertEqual([l["name"] for l in state["lanes"]], ["10526"])
        self.assertIn("load", state["machine"])

    def test_renders_in_every_mode_and_size(self):
        self.slots("acquire", "agent", lane="10526", check=0)
        for args, size in [((), (45, 160)), (("--unicode",), (45, 160)), (("--transparent",), (24, 80)),
                           ((), (8, 30))]:
            with self.subTest(args=args, size=size):
                code, out = self.dash(*args, keys=["j", "?", "?", "d", "q"], rows=size[0], cols=size[1])
                self.assertEqual(code, 0)
                if size[0] >= 10:
                    for word in ("lanes", "resources", "10526"):
                        self.assertIn(word, out)
                else:
                    self.assertIn("terminal too small", out)

    def test_release_from_the_dashboard_asks_first(self):
        self.slots("acquire", "agent", lane="10526", check=0)
        # rows with actions: lane 10526, the agent pool, its lease ...
        code, _ = self.dash(keys=["j", "j", "r", "n", "q"])
        self.assertEqual(code, 0)
        self.assertEqual(self.resource("agent")["in_use"], 1, "n keeps it")
        code, _ = self.dash(keys=["j", "j", "r", "y", "q"])
        self.assertEqual(code, 0)
        self.assertEqual(self.resource("agent")["in_use"], 0)
        log = subprocess.run(["sqlite3", str(self.home / "state.db"), "select row from admin_log"],
                             capture_output=True, text=True)
        if log.returncode == 0:
            self.assertIn("dashboard", log.stdout)


class ActionsTest(World):
    def actions(self):
        from slots import cli
        ctx = cli.Ctx(cli.parser().parse_args(["--config", str(self.config), "status"]))
        ctx.paths = type(ctx.paths)(self.home)
        return dashboard.Actions(ctx)

    def test_release_dequeue_promote_resize(self):
        a = self.actions()
        self.slots("acquire", "e2e", lane="1", check=0)
        w2 = self.spawn("acquire", "e2e", "--wait", lane="2")
        self.until(lambda: self.queue_len("e2e") == 1, what="w2")
        w3 = self.spawn("acquire", "e2e", "--wait", lane="3")
        self.until(lambda: self.queue_len("e2e") == 2, what="w3")
        r = self.resource("e2e")
        third = next(w for w in r["queue"] if w["owner"]["lane"] == "3")
        self.assertIn("moved 3 to the front", a.promote(third))
        self.assertEqual([w["owner"]["lane"] for w in self.resource("e2e")["queue"]], ["3", "2"])
        self.assertIn("released e2e (1)", a.release(r, r["holders"][0]))
        out3, _ = w3.communicate(timeout=10)
        self.assertIn("3 took e2e", out3)
        second = self.resource("e2e")["queue"][0]
        self.assertIn("removed 2 from the queue", a.dequeue(second))
        self.assertEqual(w2.wait(timeout=10), 1)

        agent = self.resource("agent")
        self.assertIn("[pool.agent] size: 2 -> 3", a.resize(agent, +1))
        self.until(lambda: self.resource("agent")["size"] == 3, what="reload")


if __name__ == "__main__":
    unittest.main()
