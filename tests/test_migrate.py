"""`slots migrate`: reads the old layout, imports live holders, drops dead ones, touches nothing."""
import shutil
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

from tests.helpers import SRC, World


def files_under(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class MigrateTest(World):
    def setUp(self):
        super().setUp()
        self.old = self.dir / "old"
        agent = self.old / ".agent-slots"
        locks = self.old / "ta-e2e" / ".locks"
        for d in (agent / "slot-1", agent / "slot-4", agent / "e2e", locks):
            d.mkdir(parents=True)
        (agent / "slot-1/owner").write_text("10526\n")
        (agent / "slot-1/since").write_text("2026-11-15T14:02Z\n")
        (agent / "slot-4/owner").write_text("11852\n")
        (agent / "slot-4/since").write_text("2026-11-15T15:30Z\n")
        (agent / "e2e/owner").write_text("10526\n")
        (agent / "e2e/since").write_text("2026-11-15T16:00Z\n")
        (agent / "e2e-queue").write_text("11852\n11860\n")
        (agent / "vault-ok").write_text("2026-11-16T00:00:00Z\n")
        self.live = subprocess.Popen(["sleep", "60"])
        self.procs.append(self.live)
        dead = subprocess.Popen(["true"])
        dead.wait()
        (locks / "slot-2.lock").touch()
        (locks / "slot-2.lock.holder").write_text(f"11865 vitest (pid {self.live.pid}, since 10:11:12Z)\n")
        (locks / "slot-3.lock.holder").write_text(f"11860 unit (pid {dead.pid}, since 09:00:00Z)\n")
        (locks / "next-mock-core.lock.holder").write_text(f"10526 tests/a.spec (pid {self.live.pid}, since 10:00:00Z)\n")
        (locks / "mystery.lock.holder").write_text(f"x (pid {self.live.pid}, since 10:00:00Z)\n")
        (locks / f".started.{dead.pid}").touch()
        self.before = files_under(self.old)

    def test_dry_run_changes_nothing(self):
        r = self.slots("migrate", "--from", str(self.old), check=0)
        out = r.stdout
        self.assertIn("dry run: nothing changed. 5 lease(s) would be imported", out)
        self.assertRegex(out, r"import\s+agent\s+10526\s+no pid recorded")
        self.assertRegex(out, r"import\s+agent\s+11852")
        self.assertRegex(out, r"import\s+e2e\s+10526")
        self.assertRegex(out, rf"import\s+test\s+11865 vitest\s+pid {self.live.pid} is running")
        self.assertRegex(out, r"drop\s+test\s+11860 unit\s+pid \d+ is gone")
        self.assertRegex(out, r"note\s+e2e queue\s+11852 11860\s+not imported")
        self.assertRegex(out, r"note\s+mystery\s+x\s+not imported: no such lock")
        self.assertIn("vault-ok", out)
        self.assertEqual(self.resource("agent")["in_use"], 0)
        self.assertEqual(files_under(self.old), self.before)

    def test_apply_imports_live_holders_once(self):
        r = self.slots("migrate", "--from", str(self.old), "--apply", check=0)
        self.assertIn("imported agent slot 1 for 10526 (manual lease)", r.stdout)
        self.assertIn("imported agent slot 2 for 11852 (manual lease)", r.stdout)
        self.assertIn("imported test slot 1 for 11865 vitest (pid lease)", r.stdout)
        self.assertIn("imported next-mock-core for 10526 tests/a.spec (pid lease)", r.stdout)
        self.assertEqual(files_under(self.old), self.before, "old files left as they are")

        agent = self.resource("agent")
        self.assertEqual([h["owner"]["lane"] for h in agent["holders"]], ["10526", "11852"])
        since = agent["holders"][0]["since"]
        self.assertEqual(time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(since)), "2026-11-15T14:02Z")
        test = self.resource("test")["holders"][0]
        self.assertEqual((test["owner"]["lane"], test["owner"]["pid"], test["kind"]), ("11865", self.live.pid, "pid"))
        self.assertEqual(self.resource("e2e")["holders"][0]["owner"]["lane"], "10526")

        r = self.slots("migrate", "--from", str(self.old), "--apply", check=0)
        self.assertEqual(r.stdout.count("skipped"), 5)
        self.assertEqual(self.resource("agent")["in_use"], 2)

        # The imported lane releases its slot like any other; the pid lease ends with its process.
        self.slots("release", "agent", lane="10526", check=0)
        self.live.kill()
        self.live.wait()
        self.until(lambda: self.resource("test")["in_use"] == 0, timeout=6, what="pid lease to end")

    def test_missing_root(self):
        self.slots("migrate", "--from", str(self.dir / "nope"), check=2)


@unittest.skipUnless(shutil.which("node"), "needs node")
class NodeClientTest(World):
    CLIENT = Path(__file__).resolve().parent.parent / "clients" / "node" / "lock.mjs"

    def node(self, script: str) -> subprocess.Popen:
        env = {**self.env, "SLOTS_BIN": f"{sys.executable} -m slots"}
        p = subprocess.Popen(["node", "--input-type=module", "-e", textwrap.dedent(script)], env=env, cwd=self.dir,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.procs.append(p)
        return p

    def test_hold_wait_release_and_crash(self):
        a = self.node(f"""
            import {{ holdLock, releaseLocks }} from '{self.CLIENT}';
            await holdLock('next-mock-core', '1 a.spec');
            console.log('a held');
            await new Promise((r) => setTimeout(r, 60_000));
        """)
        self.assertEqual(a.stdout.readline().strip(), "a held")
        self.assertEqual(self.resource("next-mock-core")["holders"][0]["owner"]["label"], "1 a.spec")
        b = self.node(f"""
            import {{ holdLock, releaseLocks, heldLocks }} from '{self.CLIENT}';
            let waited = '';
            let extended = 0;
            await holdLock('next-mock-core', '2 b.spec', {{
              onWait: (who) => {{ waited = who; }},
              whileWaiting: () => {{ const t = Date.now(); return () => {{ extended = Date.now() - t; }}; }},
            }});
            console.log('b held after waiting on', waited, 'extended', extended > 1000);
            await holdLock('next-mock-core', '2 b.spec');
            console.log(JSON.stringify(heldLocks()));
            await releaseLocks('2 b.spec');
            console.log('b released');
        """)
        self.until(lambda: self.queue_len("next-mock-core") == 1, what="b to queue")
        time.sleep(1.5)
        a.kill()   # kill -9 of node: its `slots run` child sees stdin close and lets go
        out, errs = b.communicate(timeout=20)
        self.assertEqual(b.returncode, 0, errs)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^b held after waiting on 1 a\.spec since \d\d:\d\d extended true$")
        self.assertEqual(lines[1], '[["2 b.spec","next-mock-core"]]')
        self.assertEqual(lines[2], "b released")
        self.until(lambda: self.resource("next-mock-core")["in_use"] == 0, what="released")

    def test_unknown_lock_rejects(self):
        p = self.node(f"""
            import {{ holdLock }} from '{self.CLIENT}';
            try {{ await holdLock('nope', 'x'); }} catch (e) {{ console.log(e.message); }}
        """)
        out, _ = p.communicate(timeout=20)
        self.assertIn("nope lock failed (slots exit 2)", out)


if __name__ == "__main__":
    unittest.main()
