"""The broker's rules, with a fake clock and fake process table."""
import unittest

from slots.broker import RECLAIM_GRACE, Broker, Owner, Refused
from slots.config import Resource


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def mk(ttl=None, alive=None):
    clock = Clock()
    res = {"agent": Resource("agent", "pool", 2, "session", ttl), "test": Resource("test", "pool", 2),
           "e2e": Resource("e2e", "lock"), "x": Resource("x", "lock")}
    live = alive if alive is not None else set()
    b = Broker(res, clock=clock, alive=lambda pid, start: pid in live)
    return b, clock, live


def lane(t, session=None, pid=None):
    return Owner(lane=t, session=session, pid=pid, label=t)


class BrokerTest(unittest.TestCase):
    def test_pool_limit_and_units(self):
        b, _, _ = mk()
        s1 = b.request(["agent"], lane("1"), conn="c1")
        s2 = b.request(["agent"], lane("2"), conn="c2")
        s3 = b.request(["agent"], lane("3"), conn="c3")
        self.assertEqual([s1[0], s2[0], s3[0]], ["granted", "granted", "busy"])
        self.assertEqual([l.unit for l in s1[2] + s2[2]], [1, 2])
        self.assertEqual(b.waiters, [], "a request that doesn't wait leaves no waiter")

    def test_fifo_order(self):
        b, _, _ = mk()
        b.request(["e2e"], lane("0"), conn="h")
        order = []
        for t in "12345":
            st, w, _, _ = b.request(["e2e"], lane(t), conn=t, wait=True)
            self.assertEqual(st, "queued")
            self.assertEqual(b.position(w), int(t))
        conn = "h"
        for _ in range(5):
            out = b.drop_conn(conn)
            self.assertEqual(len(out.grants), 1)
            g = out.grants[0]
            order.append(g.waiter.owner.lane)
            conn = g.waiter.conn
        self.assertEqual(order, list("12345"))

    def test_no_wait_request_does_not_jump_the_queue(self):
        b, _, _ = mk()
        b.request(["e2e"], lane("0"), conn="h")
        b.request(["e2e"], lane("1"), conn="w", wait=True)
        out = b.drop_conn("h")
        self.assertEqual(out.grants[0].waiter.owner.lane, "1")
        self.assertEqual(b.request(["e2e"], lane("2"), conn="n")[0], "busy")

    def test_multi_resource_no_starvation_no_deadlock(self):
        b, _, _ = mk()
        b.request(["e2e"], lane("a"), conn="a")           # a holds e2e
        b.request(["x"], lane("b"), conn="b")             # b holds x
        st1 = b.request(["e2e", "x"], lane("c"), conn="c", wait=True)   # c wants both
        st2 = b.request(["x", "e2e"], lane("d"), conn="d", wait=True)   # d wants both, other order
        self.assertEqual((st1[0], st2[0]), ("queued", "queued"))
        out = b.drop_conn("b")                            # x frees: c is first in line but still needs e2e
        self.assertEqual(out.grants, [])
        st = b.request(["x"], lane("e"), conn="e")        # x is free, but reserved for c
        self.assertEqual(st[0], "busy")
        out = b.drop_conn("a")
        self.assertEqual([g.waiter.owner.lane for g in out.grants], ["c"])
        out = b.drop_conn("c")
        self.assertEqual([g.waiter.owner.lane for g in out.grants], ["d"])

    def test_waiter_blocked_on_one_resource_does_not_block_unrelated(self):
        b, _, _ = mk()
        b.request(["e2e"], lane("a"), conn="a")
        b.request(["e2e"], lane("b"), conn="b", wait=True)
        self.assertEqual(b.request(["test"], lane("c"), conn="c")[0], "granted")

    def test_only_the_holder_releases(self):
        b, _, _ = mk()
        b.request(["agent"], lane("1", session="s1"), conn="c", idempotent=True)
        b.detach(b.leases[0].token, "manual")
        self.assertEqual(b.release(["agent"], lane("2"))[0], [], "lane 2 holds no agent slot: nothing to do")
        with self.assertRaises(Refused):
            b.release(["agent"], lane("1", session="other-session"))
        released, _ = b.release(["agent"], lane("1", session="s1"))
        self.assertEqual(len(released), 1)
        self.assertEqual(b.release(["agent"], lane("1"))[0], [], "nothing held: a no-op, not a refusal")
        b.request(["agent"], lane("3"), conn="c3")
        self.assertEqual(b.release(["agent"], lane("1"))[0], [], "another lane's pool unit isn't a refusal")
        b.request(["e2e"], lane("3"), conn="c3")
        with self.assertRaisesRegex(Refused, "held by 3"):
            b.release(["e2e"], lane("1"))

    def test_admin_release(self):
        b, _, _ = mk()
        b.request(["agent"], lane("1"), conn="c")
        released, _ = b.release(["agent"], Owner(lane="admin"), admin=True)
        self.assertEqual(len(released), 1)
        self.assertEqual(b.events[-1]["kind"], "admin-release")

    def test_idempotent_acquire(self):
        b, _, _ = mk()
        st, _, leases, _ = b.request(["agent"], lane("1"), conn="c", idempotent=True)
        b.detach(leases[0].token, "manual")
        st, _, leases2, _ = b.request(["agent"], lane("1"), conn="c2", idempotent=True)
        self.assertEqual(st, "already")
        self.assertEqual(leases2[0].id, leases[0].id)
        self.assertEqual(len(b.leases), 1)

    def test_conn_lease_ends_with_connection(self):
        b, _, _ = mk()
        b.request(["test"], lane("1"), conn="c")
        out = b.drop_conn("c")
        self.assertEqual(len(out.released), 1)
        self.assertEqual(b.leases, [])

    def test_detached_lease_survives_connection(self):
        b, _, _ = mk()
        st, _, leases, _ = b.request(["agent"], lane("1"), conn="c")
        b.detach(leases[0].token, "manual")
        b.drop_conn("c")
        self.assertEqual(len(b.leases), 1)

    def test_pid_lease_ends_when_pid_dies(self):
        b, _, live = mk(alive={42})
        st, _, leases, _ = b.request(["agent"], lane("1", pid=42), conn="c")
        b.detach(leases[0].token, "pid")
        b.request(["agent"], lane("2"), conn="c2")
        b.request(["agent"], lane("3"), conn="c3", wait=True)
        self.assertEqual(b.sweep().released, [])
        live.discard(42)
        out = b.sweep()
        self.assertEqual([l.owner.lane for l in out.released], ["1"])
        self.assertEqual([g.waiter.owner.lane for g in out.grants], ["3"])

    def test_no_expiry_by_default(self):
        b, clock, _ = mk()
        st, _, leases, _ = b.request(["agent"], lane("1"), conn="c")
        b.detach(leases[0].token, "manual")
        clock.t += 7 * 86400
        self.assertEqual(b.sweep().released, [])

    def test_opt_in_ttl(self):
        b, clock, _ = mk(ttl=3600)
        st, _, leases, _ = b.request(["agent"], lane("1"), conn="c")
        b.detach(leases[0].token, "manual")
        clock.t += 3000
        self.assertEqual(b.touch(lane("1")), 1)
        clock.t += 3000
        self.assertEqual(b.sweep().released, [], "activity resets the clock")
        clock.t += 3601
        self.assertEqual(len(b.sweep().released), 1)

    def test_timeout(self):
        b, clock, _ = mk()
        b.request(["e2e"], lane("a"), conn="a")
        b.request(["e2e"], lane("b"), conn="b", wait=True, timeout=5)
        clock.t += 4
        self.assertEqual(b.tick().timed_out, [])
        clock.t += 2
        self.assertEqual([w.owner.lane for w in b.tick().timed_out], ["b"])
        self.assertEqual(b.waiters, [])

    def test_restart_keeps_state_and_reclaims(self):
        b, clock, _ = mk()
        _, _, held, _ = b.request(["e2e"], lane("a"), conn="a", token="ta")
        b.request(["e2e"], lane("b"), conn="b", token="tb", wait=True)
        _, _, agent, _ = b.request(["agent"], lane("c"), conn="c", token="tc")
        b.detach("tc", "manual")
        rows = b.rows()

        b2, clock2, _ = mk()
        clock2.t = clock.t
        b2.load_rows(rows, lambda n: None)
        self.assertEqual(len(b2.leases), 2)
        self.assertEqual(b2.reclaim("ta", "a2")[0].id, held[0].id)
        st, w, _, _ = b2.request(["e2e"], lane("b"), conn="b2", token="tb", wait=True)
        self.assertEqual(st, "queued", "the waiter gets its place back")
        out = b2.drop_conn("a2")
        self.assertEqual([g.waiter.owner.lane for g in out.grants], ["b"])
        self.assertEqual(len([l for l in b2.leases if l.resource == "agent"]), 1, "detached lease kept")

    def test_restart_drops_unclaimed(self):
        b, clock, _ = mk()
        b.request(["e2e"], lane("a"), conn="a", token="ta")
        b.request(["e2e"], lane("b"), conn="b", token="tb", wait=True)
        b2, clock2, _ = mk()
        clock2.t = clock.t
        b2.load_rows(b.rows(), lambda n: None)
        clock2.t += RECLAIM_GRACE + 0.1
        out = b2.tick()
        self.assertEqual(len(out.released), 1)
        self.assertEqual(b2.leases, [], "the orphan waiter was granted, then lost too")
        self.assertEqual(b2.waiters, [])

    def test_granted_while_reconnecting(self):
        b, clock, _ = mk()
        b.request(["e2e"], lane("a"), conn="a", token="ta")
        b.request(["e2e"], lane("b"), conn="b", token="tb", wait=True)
        b2, clock2, _ = mk()
        clock2.t = clock.t
        b2.load_rows(b.rows(), lambda n: None)
        b2.reclaim("ta", "a2")
        b2.drop_conn("a2")                       # a finishes before b reconnects: b is granted, orphaned
        st, _, leases, _ = b2.request(["e2e"], lane("b"), conn="b2", token="tb", wait=True)
        self.assertEqual(st, "granted")
        self.assertIsNone(leases[0].orphan_until)

    def test_shrinking_a_pool(self):
        b, _, _ = mk()
        b.request(["test"], lane("1"), conn="1")
        b.request(["test"], lane("2"), conn="2")
        b.request(["test"], lane("3"), conn="3", wait=True)
        b.update_resources({**b.resources, "test": Resource("test", "pool", 1)})
        self.assertEqual(b.drop_conn("1").grants, [], "still at the new limit")
        self.assertEqual(len(b.drop_conn("2").grants), 1)

    def test_snapshot(self):
        b, _, _ = mk()
        b.request(["agent"], lane("1"), conn="c")
        b.request(["e2e"], lane("1"), conn="c")
        b.request(["e2e"], lane("2"), conn="d", wait=True)
        snap = {r["name"]: r for r in b.snapshot()["resources"]}
        self.assertEqual(snap["agent"]["in_use"], 1)
        self.assertEqual(snap["e2e"]["queue"][0]["owner"]["lane"], "2")


if __name__ == "__main__":
    unittest.main()
