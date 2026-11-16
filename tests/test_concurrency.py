"""Real processes against a real daemon: races, FIFO order, kill -9, restarts."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.helpers import World

# A command that records how many runs overlap it: appends "start <t>" / "end <t>" lines.
RECORD = ("import sys, time, os; f = sys.argv[1]; open(f, 'a').write(f'start {time.time()}\\n'); "
          "time.sleep(float(sys.argv[2])); open(f, 'a').write(f'end {time.time()}\\n')")


def max_overlap(path) -> int:
    events = []
    for line in Path(path).read_text().splitlines():
        kind, t = line.split()
        events.append((float(t), 1 if kind == "start" else -1))
    cur = best = 0
    for _, d in sorted(events, key=lambda e: (e[0], e[1])):
        cur += d
        best = max(best, cur)
    return best


class RaceTest(World):
    def test_many_processes_racing_for_a_pool(self):
        log = self.dir / "overlap.log"
        n = 16
        procs = [self.spawn("run", "test", "-q", "--", sys.executable, "-c", RECORD, str(log), "0.15")
                 for _ in range(n)]
        for p in procs:
            out, errs = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, errs)
        self.assertEqual(sum(1 for l in log.read_text().splitlines() if l.startswith("start")), n)
        self.assertLessEqual(max_overlap(log), 2, "never more than the pool's 2 slots at once")
        self.assertEqual(self.resource("test")["in_use"], 0)

    def test_fifo_order_under_contention(self):
        holder = self.spawn("run", "e2e", "--", "sleep", "30", lane="1")
        self.until(lambda: self.resource("e2e")["in_use"] == 1, what="holder")
        log = self.dir / "order.log"
        waiters = []
        for i in range(2, 8):
            waiters.append(self.spawn("run", "e2e", "-q", "--", "sh", "-c", f"echo {i} >> {log}", lane=str(i)))
            self.until(lambda: self.queue_len("e2e") == i - 1, what=f"waiter {i} queued")
        holder.send_signal(signal.SIGTERM)
        for w in waiters:
            w.wait(timeout=30)
        self.assertEqual(log.read_text().split(), [str(i) for i in range(2, 8)])

    def test_exit_code_and_signals_pass_through(self):
        self.assertEqual(self.slots("run", "test", "--", "sh", "-c", "exit 7").returncode, 7)
        self.assertEqual(self.slots("run", "test", "--", "sh", "-c", "kill -9 $$").returncode, 137)
        self.assertEqual(self.slots("run", "test", "--", "/no/such/command").returncode, 127)


class CrashTest(World):
    def test_kill_9_of_a_holder_frees_its_slot_at_once(self):
        a = self.spawn("run", "e2e", "--", "sleep", "60", lane="1")
        self.until(lambda: self.resource("e2e")["in_use"] == 1, what="holder")
        b = self.spawn("run", "e2e", "--", "true", lane="2")
        self.until(lambda: self.queue_len("e2e") == 1, what="waiter")
        t0 = time.monotonic()
        a.kill()           # SIGKILL: no cleanup runs in the client
        b.wait(timeout=10)
        self.assertEqual(b.returncode, 0)
        self.assertLess(time.monotonic() - t0, 3)

    def test_kill_9_of_a_waiter_leaves_the_queue(self):
        self.spawn("run", "e2e", "--", "sleep", "60", lane="1")
        self.until(lambda: self.resource("e2e")["in_use"] == 1, what="holder")
        w = self.spawn("run", "e2e", "--", "true", lane="2")
        self.until(lambda: self.queue_len("e2e") == 1, what="waiter")
        w.kill()
        self.until(lambda: self.queue_len("e2e") == 0, what="queue to empty")

    def test_kill_9_of_the_daemon(self):
        # A holds e2e while B waits. The daemon is killed: A keeps its lease through the restart,
        # B keeps its place, and B runs once A is done.
        flag = self.dir / "a-may-finish"
        a = self.spawn("run", "e2e", "--", "sh", "-c", f"while [ ! -e {flag} ]; do sleep 0.05; done; exit 5", lane="1")
        self.until(lambda: self.resource("e2e")["in_use"] == 1, what="holder")
        log = self.dir / "b.log"
        b = self.spawn("run", "e2e", "--", "sh", "-c", f"echo ran >> {log}", lane="2")
        self.until(lambda: self.queue_len("e2e") == 1, what="waiter")
        old = self.daemon_pid()
        os.kill(old, signal.SIGKILL)

        # The clients reconnect, which starts a new daemon; the lease and the queue come back.
        self.until(lambda: (p := self.daemon_pid()) and p != old and self.slots("daemon", "status").returncode == 0,
                   what="new daemon")
        e2e = self.until(lambda: (r := self.resource("e2e"))["in_use"] == 1 and len(r["queue"]) == 1 and
                         not r["holders"][0].get("orphan_until") and r, what="lease reclaimed")
        self.assertEqual(e2e["holders"][0]["owner"]["lane"], "1")
        self.assertFalse(log.exists(), "B must not run while A still holds the lock")

        flag.touch()
        self.assertEqual(a.wait(timeout=20), 5)
        self.assertEqual(b.wait(timeout=20), 0)
        self.assertEqual(log.read_text(), "ran\n")

    def test_daemon_restart_with_nobody_reconnecting_drops_conn_leases(self):
        a = self.spawn("run", "e2e", "--", "sleep", "60", lane="1")
        self.until(lambda: self.resource("e2e")["in_use"] == 1, what="holder")
        self.slots("acquire", "agent", lane="3", check=0)
        os.kill(self.daemon_pid(), signal.SIGSTOP)      # freeze the daemon so the client can't help
        a.kill()
        a.wait()
        os.kill(self.daemon_pid(), signal.SIGKILL)
        self.until(lambda: self.resource("e2e")["in_use"] == 0, timeout=20, what="orphan lease dropped")
        self.assertEqual(self.resource("agent")["in_use"], 1, "the detached agent slot is kept")

    def test_pid_lease_ends_when_its_process_dies(self):
        sleeper = subprocess.Popen(["sleep", "60"])
        self.procs.append(sleeper)
        self.slots("acquire", "agent", "--pid", str(sleeper.pid), lane="1", check=0)
        self.assertEqual(self.resource("agent")["holders"][0]["kind"], "pid")
        sleeper.kill()
        sleeper.wait()
        self.until(lambda: self.resource("agent")["in_use"] == 0, timeout=6, what="pid sweep")


class IdentityTest(World):
    def test_acquire_release_and_wrong_identity(self):
        r = self.slots("acquire", "agent", lane="1", check=0)
        self.assertIn("1 took agent slot 1", r.stdout)
        r = self.slots("acquire", "agent", lane="1", check=0)
        self.assertIn("already holds agent slot 1", r.stdout)
        r = self.slots("release", "agent", lane="2", check=0)
        self.assertIn("2 holds nothing to release", r.stdout)
        self.slots("acquire", "e2e", lane="1", check=0)
        r = self.slots("release", "e2e", lane="2", check=1)
        self.assertIn("refused", r.stderr)
        self.assertIn("held by 1", r.stderr)
        self.slots("release", "e2e", lane="1", check=0)
        r = self.slots("release", "agent", lane="1", env={"SLOTS_SESSION": "other"}, check=0)
        self.assertIn("released", r.stdout, "the lease has no session, so the lane's own sessions may release it")
        self.assertEqual(self.resource("agent")["in_use"], 0)

    def test_session_identity(self):
        self.slots("acquire", "agent", lane="1", env={"SLOTS_SESSION": "s-one"}, check=0)
        self.slots("release", "agent", lane="1", env={"SLOTS_SESSION": "s-two"}, check=1)
        self.slots("release", "--all", lane="1", env={"SLOTS_SESSION": "s-one"}, check=0)
        self.assertEqual(self.resource("agent")["in_use"], 0)

    def test_busy_and_timeout(self):
        self.slots("acquire", "agent", lane="1", check=0)
        self.slots("acquire", "agent", lane="2", check=0)
        r = self.slots("acquire", "agent", lane="3", check=1)
        self.assertIn("agent not free", r.stderr)
        t0 = time.monotonic()
        r = self.slots("acquire", "agent", "--wait", "--timeout", "0.5", lane="3", check=124)
        self.assertIn("waiting for agent (2/2 in use", r.stderr)
        self.assertLess(time.monotonic() - t0, 5)

    def test_wait_is_woken_at_once(self):
        self.slots("acquire", "agent", lane="1", check=0)
        self.slots("acquire", "agent", lane="2", check=0)
        w = self.spawn("acquire", "agent", "--wait", lane="3")
        self.until(lambda: self.queue_len("agent") == 1, what="waiter")
        t0 = time.monotonic()
        self.slots("release", "agent", lane="1", check=0)
        out, _ = w.communicate(timeout=10)
        self.assertIn("3 took agent slot 1", out)
        self.assertLess(time.monotonic() - t0, 1.5)

    def test_admin_release_and_dequeue(self):
        self.slots("acquire", "e2e", lane="1", check=0)
        w = self.spawn("acquire", "e2e", "--wait", lane="2")
        self.until(lambda: self.queue_len("e2e") == 1, what="waiter")
        self.slots("admin", "dequeue", "e2e", "--holder", "2", check=1)   # no terminal and no --yes
        self.slots("admin", "dequeue", "e2e", "--holder", "2", "--yes", check=0)
        self.assertEqual(w.wait(timeout=10), 1)
        self.slots("admin", "release", "e2e", "--yes", "--reason", "test", check=0)
        self.assertEqual(self.resource("e2e")["in_use"], 0)

    def test_family_lock(self):
        r = self.slots("acquire", "next-employee:Someone@Example.com", lane="1", check=0)
        self.assertIn("took next-employee-", r.stdout)
        self.slots("acquire", "next-employee:someone@example.com ", lane="2", check=1)
        self.slots("acquire", "next-employee:other@example.com", lane="2", check=0)

    def test_status_text_and_json(self):
        self.slots("acquire", "agent", lane="301", check=0)
        r = self.slots("status", check=0)
        self.assertIn("AGENT  pool  1/2 in use", r.stdout)
        self.assertIn("301", r.stdout)
        state = json.loads(self.slots("status", "--json", check=0).stdout)
        self.assertIn("daemon", state)
        self.assertEqual({x["name"] for x in state["resources"]}, {"agent", "test", "e2e", "next-mock-core"})


class ReloadTest(World):
    def test_config_change_applies_live(self):
        self.slots("acquire", "agent", lane="1", check=0)
        self.slots("acquire", "agent", lane="2", check=0)
        self.slots("acquire", "agent", lane="3", check=1)
        self.config.write_text(self.config.read_text().replace("size = 2\nhold", "size = 3\nhold"))
        self.until(lambda: next(r for r in self.state()["resources"] if r["name"] == "agent")["size"] == 3,
                   what="reload")
        self.slots("acquire", "agent", lane="3", check=0)


class TtlTest(World):
    extra_config = """
    [pool.short]
    size = 1
    ttl = "1s"
    """

    def test_opt_in_ttl(self):
        self.slots("acquire", "short", lane="1", check=0)
        self.until(lambda: self.resource("short")["in_use"] == 0, timeout=8, what="ttl expiry")


if __name__ == "__main__":
    import unittest
    unittest.main()
