"""Watchers run in the daemon and set flags: credential, lane-setup, python, and their triggers."""
import json
import subprocess
import sys
import textwrap
import time
import tomllib
import unittest
from datetime import datetime, timedelta, timezone

from slots import config, watchers
from tests.helpers import World


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class WatcherWorld(World):
    def flags(self) -> dict:
        return {f["name"]: f for f in self.state()["flags"]}

    def flag(self, name, state=None, timeout=15, text=None):
        return self.until(lambda: (f := self.flags().get(name)) and (state is None or f["state"] == state)
                          and (text is None or text in f["text"]) and f,
                          timeout=timeout, what=f"flag {name} {state or ''} {text or ''}")

    def kick(self):
        self.slots("status", check=0)   # starts the daemon, which starts the watchers


class CredentialTest(WatcherWorld):
    @property
    def extra_config(self):
        return f"""
        [watcher.vault]
        kind = "credential"
        command = "test -f {{root}}/../token.json && cat {{root}}/../token.json"
        expiry = "data.expire_time"
        warn_before = "30m"
        every = "1s"

        [watcher.sso]
        kind = "credential"
        command = "exit 0"
        every = "1s"
        """

    def test_valid_expiring_expired_and_missing(self):
        token = self.dir / "token.json"
        token.write_text(json.dumps({"data": {"expire_time": iso(timedelta(hours=3))}}))
        self.kick()
        f = self.flag("vault", "ok")
        self.assertRegex(f["text"], r"^vault: valid until \d\d:\d\d \(in 2h5\dm\)$")
        self.assertEqual(self.flag("sso", "ok")["text"], "sso: valid")
        token.write_text(json.dumps({"data": {"expire_time": iso(timedelta(minutes=10))}}))
        self.assertIn("in 9m", self.flag("vault", "warn", text="in 9m")["text"])
        token.write_text(json.dumps({"data": {"expire_time": iso(-timedelta(minutes=1))}}))
        self.assertIn("vault: expired at", self.flag("vault", "bad", text="expired")["text"])
        token.unlink()
        self.assertEqual(self.flag("vault", "bad", text="not valid")["text"], "vault: not valid (exit 1)")
        out = self.slots("status", check=0).stdout
        self.assertIn("[hot] vault: not valid (exit 1)", out)
        self.assertIn("WATCHERS", out)
        token.write_text("{}")
        self.assertIn("no `data.expire_time`", self.flag("vault", "ok", text="no `")["text"])


class LaneSetupTest(WatcherWorld):
    @property
    def extra_config(self):
        return f"""
        [watcher.signin]
        kind = "lane-setup"
        port = "ui"
        artifact = "{{root}}/{{lane}}/auth.json"
        command = "echo run >> {{root}}/{{lane}}/runs; if [ -f {{root}}/{{lane}}/fail ]; then exit 3; fi; touch {{root}}/{{lane}}/auth.json; echo 1 passed"
        cwd = "{{root}}/{{lane}}"
        env = {{ TICKET = "PDE-{{lane}}" }}
        success = " passed"
        locks = ["e2e"]
        settle = "0s"
        every = "1s"
        """

    def serve(self, port):
        p = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(p)
        import socket
        self.until(lambda: socket.socket().connect_ex(("127.0.0.1", port)) == 0, what="server up")
        return p

    def runs(self, lane="10526"):
        f = self.root / lane / "runs"
        return f.read_text().count("run") if f.exists() else 0

    def test_runs_when_the_artifact_is_older_than_the_stack(self):
        lane = self.lane("10526")
        self.lane("11852")   # its port isn't up: never run
        port = int(self.slots("port", "10526", "ui", check=0).stdout)
        server = self.serve(port)
        self.assertIn("ran it for 10526", self.flag("signin:10526", "ok")["text"])
        self.assertTrue((lane / "auth.json").exists())
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.flag("signin:10526")["state"], "ok")
        time.sleep(2.5)
        self.assertEqual(self.runs(), 1, "the artifact is newer than the stack: nothing to do")
        self.assertIn("up to date", self.flags()["signin:10526"]["text"])
        self.assertEqual(self.runs("11852"), 0)

        # The stack restarts: its process is newer than the artifact, so it runs again.
        server.kill()
        server.wait()
        time.sleep(1.1)
        self.serve(port)
        self.until(lambda: self.runs() == 2, what="a second run")

    def test_failure_is_flagged_and_retried(self):
        lane = self.lane("10526")
        (lane / "fail").touch()
        port = int(self.slots("port", "10526", "ui", check=0).stdout)
        self.serve(port)
        f = self.flag("signin:10526", "bad")
        self.assertIn("failed for 10526 (exit 3); will retry", f["text"])
        self.until(lambda: self.runs() >= 2, what="a retry")
        (lane / "fail").unlink()
        self.flag("signin:10526", "ok", text="ran it")
        log = (self.home / "watchers" / "signin-10526.log").read_text()
        self.assertIn("exit 3", log)

    def test_waits_for_its_locks(self):
        self.lane("10526")
        self.slots("acquire", "e2e", lane="99", check=0)
        port = int(self.slots("port", "10526", "ui", check=0).stdout)
        self.serve(port)
        self.until(lambda: self.queue_len("e2e") == 1, what="the setup run to queue for e2e")
        self.assertEqual(self.runs(), 0)
        self.slots("release", "e2e", lane="99", check=0)
        self.flag("signin:10526", "ok", text="ran it")
        self.assertEqual(self.runs(), 1)


class EventTest(WatcherWorld):
    @property
    def extra_config(self):
        return f"""
        [watcher.on-e2e]
        kind = "credential"
        command = "echo x >> {{root}}/../e2e-runs"
        on = ["lease:e2e"]

        [watcher.after]
        kind = "credential"
        command = "echo x >> {{root}}/../after-runs"
        on = "flag:on-e2e"

        [watcher.custom]
        kind = "python"
        path = "{{root}}/../w.py"
        every = "1s"

        [watcher.broken]
        kind = "python"
        path = "{{root}}/../nope.py"
        on = "start"
        """

    def count(self, name):
        f = self.dir / name
        return f.read_text().count("x") if f.exists() else 0

    def test_triggers_python_and_errors(self):
        (self.dir / "w.py").write_text(textwrap.dedent("""
            def check(ctx):
                held = sum(r["in_use"] for r in ctx["state"]["resources"])
                return [{"name": "held", "state": "warn" if held else "ok", "text": f"{held} held"}]
        """))
        self.kick()
        self.assertEqual(self.flag("custom:held", "ok")["text"], "0 held")
        self.assertIn("the watcher failed", self.flag("broken", "bad")["text"])
        self.assertEqual(self.count("e2e-runs"), 0, "not run until its event")
        self.slots("run", "e2e", "--", "true", check=0)
        self.until(lambda: self.count("e2e-runs") == 1, what="lease:e2e trigger")
        self.until(lambda: self.count("after-runs") == 1, what="flag:on-e2e trigger (its flag appeared)")
        self.slots("acquire", "agent", lane="1", check=0)
        self.flag("custom:held", "warn")

    def test_run_now_and_listing(self):
        (self.dir / "w.py").write_text("def check(ctx):\n    return {'state': 'ok', 'text': 'fine'}\n")
        self.kick()
        self.slots("watchers", "--run", "on-e2e", check=0)
        self.until(lambda: self.count("e2e-runs") == 1, what="run now")
        self.slots("watchers", "--run", "nope", check=2)
        self.flag("custom", "ok")
        out = self.slots("watchers", check=0).stdout
        self.assertIn("custom  (python; next in", out)
        self.assertIn("ok    fine", out)
        self.assertIn("on-e2e  (credential; on lease:e2e", out)


class ConfigTest(unittest.TestCase):
    def test_validation(self):
        for bad, msg in [('kind = "nope"', "kind must be"), ('kind = "credential"', "needs `command`"),
                         ('kind = "credential"\ncommand = "x"\non = "sometimes"', "isn't start"),
                         ('kind = "lane-setup"\nport = "ui"\ncommand = "x"', "needs `artifact`")]:
            with self.subTest(bad), self.assertRaisesRegex(config.ConfigError, msg):
                config.from_dict(tomllib.loads(f"[watcher.w]\n{bad}\n"))
        w = config.from_dict(tomllib.loads('[watcher.w]\nkind="credential"\ncommand="x"\n')).watchers["w"]
        self.assertEqual((w["every"], w["timeout"], w["warn_before"]), (60.0, 30.0, 1800.0))

    def test_helpers(self):
        self.assertEqual(watchers.dig({"data": {"expire_time": "t"}}, "data.expire_time"), "t")
        self.assertIsNone(watchers.dig({"data": {}}, "data.expire_time"))
        self.assertEqual(watchers.dig({"a": [{"b": 1}]}, "a.0.b"), 1)
        self.assertAlmostEqual(watchers.parse_time("2026-11-16T10:00:00.123456789Z"),
                               datetime(2026, 11, 16, 10, 0, 0, 123456, tzinfo=timezone.utc).timestamp(), places=3)
        self.assertEqual(watchers.template("{root}/{lane}/x-{lane}", root="/r", lane="7"), "/r/7/x-7")


if __name__ == "__main__":
    unittest.main()
