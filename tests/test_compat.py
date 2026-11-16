"""The shims keep the old scripts' arguments, output phrases and exit codes."""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from slots import shims
from tests.helpers import World

REFERENCE = Path(__file__).resolve().parent.parent / "reference"
TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\dZ")

# agent-slot.sh with 7 slots: (arguments, stdout, exit code), timestamps shown as TS.
AGENT_TRANSCRIPT = [
    ("acquire 101", "101 took slot 1\n", 0),
    ("acquire 101", "101 already holds slot 1\n", 0),
    *[(f"acquire {t}", f"{t} took slot {t - 100}\n", 0) for t in range(102, 108)],
    ("acquire 108", "all 7 slots are taken:\n" + "".join(f"slot {i}: {100 + i} since TS\n" for i in range(1, 8)), 1),
    ("release 103", "103 released slot 3\n", 0),
    ("release 103", "103 holds no slot\n", 0),
    ("acquire 108", "108 took slot 3\n", 0),
    ("e2e-acquire 101", "101 took the e2e lock\n", 0),
    ("e2e-acquire 101", "101 already holds the e2e lock\n", 0),
    ("e2e-acquire 102", "e2e lock held by 101 since TS; queue: \n", 1),
    ("status", "".join(f"slot {i}: {100 + i if i != 3 else 108} since TS\n" for i in range(1, 8))
     + "e2e: 101 since TS\ne2e queue: \n", 0),
    ("e2e-release 102", "102 doesn't hold the e2e lock (left the queue)\n", 0),
    ("e2e-release 101", "101 released the e2e lock\n", 0),
    ("release 101", "101 released slot 1\n", 0),
    ("status", "slot 1: free\n" + "".join(f"slot {i}: {100 + i if i != 3 else 108} since TS\n" for i in range(2, 8))
     + "e2e: free\ne2e queue: \n", 0),
    ("bogus", "", 2),
]


class ShimWorld(World):
    agent_size = 7

    def setUp(self):
        super().setUp()
        self.bin = self.dir / "bin"
        shims.install(self.bin, self.config, use_path=False)

    def sh(self, *args, timeout=30, env=None) -> subprocess.CompletedProcess:
        return subprocess.run(list(args), cwd=self.dir, env=env or self.env, capture_output=True, text=True,
                              timeout=timeout)


class AgentSlotTest(ShimWorld):
    def run_transcript(self, script: list[str], env: dict) -> list[tuple[str, str, int]]:
        out = []
        for args, _, _ in AGENT_TRANSCRIPT:
            r = subprocess.run([*script, *args.split()], env=env, capture_output=True, text=True, timeout=30)
            out.append((args, TS.sub("TS", r.stdout), r.returncode))
        return out

    def test_transcript(self):
        got = self.run_transcript([str(self.bin / "agent-slot.sh")], self.env)
        for (args, want, code), (_, out, rc) in zip(AGENT_TRANSCRIPT, got):
            with self.subTest(args):
                self.assertEqual((out, rc), (want, code))

    def test_usage_text(self):
        r = self.sh(str(self.bin / "agent-slot.sh"))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stderr.strip(),
                         "usage: agent-slot.sh acquire|release|e2e-acquire|e2e-release <ticket> [--wait] | status")
        self.assertEqual(self.sh(str(self.bin / "agent-slot.sh"), "acquire").returncode, 1)

    def test_reference_script_gives_the_same_transcript(self):
        ref = REFERENCE / "agent-slot.sh"
        if not ref.exists():
            self.skipTest("reference/ is not in this checkout")
        home = Path(tempfile.mkdtemp(prefix="ref-", dir="/tmp"))
        # The reference's e2e-acquire exits 1, silently, when the queue file doesn't exist yet
        # (`head` fails under set -e). The shim doesn't have that bug; give the reference its file.
        (home / "Documents/dev/interface/.agent-slots").mkdir(parents=True)
        (home / "Documents/dev/interface/.agent-slots/e2e-queue").touch()
        try:
            got = self.run_transcript(["bash", str(ref)], {**os.environ, "HOME": str(home)})
        finally:
            shutil.rmtree(home, ignore_errors=True)
        self.assertEqual([(o, c) for _, o, c in got], [(o, c) for _, o, c in AGENT_TRANSCRIPT])

    def test_e2e_wait_and_queue(self):
        script = str(self.bin / "agent-slot.sh")
        self.sh(script, "e2e-acquire", "101")
        waiter = subprocess.Popen([script, "e2e-acquire", "102", "--wait"], env=self.env,
                                  stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.procs.append(waiter)
        self.until(lambda: "e2e queue: 102 \n" in self.sh(script, "status").stdout, what="queued")
        r = self.sh(script, "e2e-acquire", "103")
        self.assertEqual((TS.sub("TS", r.stdout), r.returncode), ("e2e lock held by 101 since TS; queue: 102 \n", 1))
        t0 = time.monotonic()
        self.sh(script, "e2e-release", "101")
        out, _ = waiter.communicate(timeout=10)
        self.assertEqual(out, "102 took the e2e lock\n")
        self.assertLess(time.monotonic() - t0, 2, "woken at once, not after a 30 s poll")

    def test_e2e_release_leaves_the_queue(self):
        script = str(self.bin / "agent-slot.sh")
        self.sh(script, "e2e-acquire", "101")
        waiter = subprocess.Popen([script, "e2e-acquire", "102", "--wait"], env=self.env,
                                  stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.procs.append(waiter)
        self.until(lambda: self.queue_len("e2e") == 1, what="queued")
        r = self.sh(script, "e2e-release", "102")
        self.assertEqual(r.stdout, "102 doesn't hold the e2e lock (left the queue)\n")
        self.assertEqual(waiter.wait(timeout=10), 1)
        self.assertEqual(self.queue_len("e2e"), 0)


class SlotShTest(ShimWorld):
    agent_size = 2

    def test_usage(self):
        for args in ([], ["--label", "x"], ["--bogus"]):
            r = self.sh(str(self.bin / "slot.sh"), *args)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stderr.strip(), "usage: slot.sh [--label <who>] -- <command...>")

    def test_exit_code_and_label(self):
        r = self.sh(str(self.bin / "slot.sh"), "--label", "308 vitest", "--", "sh", "-c", "exit 9")
        self.assertEqual(r.returncode, 9)

    def test_waits_for_a_test_slot(self):
        holders = [subprocess.Popen([str(self.bin / "slot.sh"), "--label", f"1{i} vitest", "--", "sleep", "30"],
                                    env=self.env, start_new_session=True) for i in range(2)]
        self.procs += holders
        self.until(lambda: self.resource("test")["in_use"] == 2, what="slots full")
        w = subprocess.Popen([str(self.bin / "slot.sh"), "--label", "12 unit", "--", "true"], env=self.env,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.procs.append(w)
        self.until(lambda: self.queue_len("test") == 1, what="waiter")
        holders[0].terminate()
        _, errs = w.communicate(timeout=15)
        self.assertEqual(w.returncode, 0)
        # The two holders race for the slots, so either may be listed first.
        one = r"1[01] vitest \(pid \d+, since \d\d:\d\d:\d\dZ\)"
        self.assertRegex(errs, rf"^waiting for a test slot \(2 in use: {one};{one}\)\n$")
        self.assertIn("10 vitest", errs)
        self.assertIn("11 vitest", errs)


class LibSlotTest(ShimWorld):
    def bash(self, script: str, **kw):
        return self.sh("bash", "-c", f"source {self.bin}/lib-slot.sh\n{script}", **kw)

    def test_next_employee_lock(self):
        email = "  Test.Person@Example.COM "
        want = "next-employee-" + hashlib.sha1(email.strip().lower().encode()).hexdigest()[:12]
        r = self.bash('next_employee_lock "$EMAIL"', env={**self.env, "EMAIL": email})
        self.assertEqual(r.stdout.strip(), want)

    def test_run_exclusive_sets_the_lock_name_and_waits(self):
        r = self.bash('run_exclusive next-mock-core "the mock core" "1 test" sh -c \'echo "$TA_EXCLUSIVE_LOCK"; exit 3\'')
        self.assertEqual((r.stdout, r.returncode), ("next-mock-core\n", 3))
        holder = subprocess.Popen(["bash", "-c", f"source {self.bin}/lib-slot.sh\n"
                                   'run_exclusive next-mock-core "the mock core" "1 spec" sleep 30'],
                                  env=self.env, start_new_session=True)
        self.procs.append(holder)
        self.until(lambda: self.resource("next-mock-core")["in_use"] == 1, what="holder")
        w = subprocess.Popen(["bash", "-c", f"source {self.bin}/lib-slot.sh\n"
                              'run_exclusive next-mock-core "the mock core" "2 spec" true'],
                             env=self.env, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.procs.append(w)
        self.until(lambda: self.queue_len("next-mock-core") == 1, what="waiter")
        os.killpg(holder.pid, 9)
        _, errs = w.communicate(timeout=15)
        self.assertEqual(w.returncode, 0)
        self.assertRegex(errs, r"^waiting for the mock core \(held by 1 spec \(pid \d+, since \d\d:\d\d:\d\dZ\)\)\n$")

    def test_run_in_slot_and_holders(self):
        r = self.bash('echo "$TA_SLOT_COUNT"; run_in_slot "1 unit" sh -c "exit 4"; echo "code $?"')
        self.assertEqual(r.stdout, "2\ncode 4\n")
        r = self.bash(f'run_in_slot "1 unit" bash -c "source {self.bin}/lib-slot.sh; slot_holders"')
        self.assertRegex(r.stdout, r"^1 unit \(pid \d+, since \d\d:\d\d:\d\dZ\)\n$")

    def test_family_lock_by_name(self):
        r = self.bash('run_exclusive "$(next_employee_lock a@b.c)" "a@b.c" "1 e2e" sh -c \'echo "$TA_EXCLUSIVE_LOCK"\'')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("next-employee-"))


class InstallTest(World):
    def test_install_keeps_existing_files(self):
        d = self.dir / "bin"
        d.mkdir()
        (d / "slot.sh").write_text("old\n")
        r = self.slots("shims", "install", "--dir", str(d), check=0)
        self.assertIn("skipped", r.stdout)
        self.assertEqual((d / "slot.sh").read_text(), "old\n")
        self.slots("shims", "install", "--dir", str(d), "--force", check=0)
        self.assertEqual([p.read_text() for p in d.glob("slot.sh.orig-*")], ["old\n"])
        self.assertIn("compat run-in-slot", (d / "slot.sh").read_text())
        self.assertTrue(os.access(d / "agent-slot.sh", os.X_OK))
        r = self.slots("shims", "install", "--dir", str(d), check=0)
        self.assertEqual(r.stdout.count("unchanged"), 3)


if __name__ == "__main__":
    import unittest
    unittest.main()
