"""The broker: pools and locks, leases with an owner, and one FIFO queue across them.

Pure state and rules, with no sockets or files, so it can be tested directly. The
daemon owns one Broker, calls it from a single event loop, persists its rows, and
tells waiting connections when they are granted.

Scheduling is a single global FIFO. Waiters are considered in arrival order; a
waiter is granted when every resource it asks for has a free unit after the
waiters before it have been served or have reserved their units. A waiter that
can't be served reserves a unit of each resource it wants, so nobody behind it
can take them first: no starvation, and no deadlock between waiters asking for
the same resources in a different order.
"""
from __future__ import annotations

import secrets
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable

from slots import proc
from slots.config import Resource

#: Seconds a connection-held lease or a waiter survives a daemon restart, for its client to reclaim it.
RECLAIM_GRACE = 10.0
#: Lease kinds: "conn" ends when its client's connection closes; "pid" when that pid dies;
#: "manual" only on release (or an admin action, or an opt-in ttl).
KINDS = ("conn", "pid", "manual")


@dataclass
class Owner:
    lane: str | None = None
    session: str | None = None
    pid: int | None = None
    pid_start: str | None = None
    host: str | None = None
    label: str | None = None

    @classmethod
    def of(cls, d: dict | None) -> Owner:
        d = d or {}
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})

    def describe(self) -> str:
        who = self.label or self.lane or "someone"
        return f"{who} (pid {self.pid})" if self.pid else who

    def same_holder(self, other: Owner) -> bool:
        """Whether `other` (a requester) may act for this owner: the same lane, and the same session
        when both name one. A request with no lane never matches."""
        if not other.lane or self.lane != other.lane:
            return False
        return not (self.session and other.session and self.session != other.session)


@dataclass
class Lease:
    id: str
    resource: str
    unit: int
    kind: str
    owner: Owner
    token: str
    since: float
    last_active: float
    conn: object | None = None
    orphan_until: float | None = None

    def row(self) -> dict:
        return _row(self, skip=("conn",))


def _row(obj, skip: tuple[str, ...]) -> dict:
    """A dataclass as a plain dict, leaving out live objects (asdict would try to copy a connection)."""
    d = {k: getattr(obj, k) for k in obj.__dataclass_fields__ if k not in skip}
    d["owner"] = asdict(d["owner"])
    return d


@dataclass
class Waiter:
    token: str
    seq: int
    resources: list[str]
    owner: Owner
    since: float
    deadline: float | None = None
    conn: object | None = None
    orphan_until: float | None = None
    position: int | None = None   # last position told to the client

    def row(self) -> dict:
        return _row(self, skip=("conn", "position"))


@dataclass
class Grant:
    waiter: Waiter
    leases: list[Lease]


@dataclass
class Outcome:
    """What a call changed, for the daemon to act on."""
    grants: list[Grant] = field(default_factory=list)
    released: list[Lease] = field(default_factory=list)
    timed_out: list[Waiter] = field(default_factory=list)
    dropped: list[Waiter] = field(default_factory=list)

    def merge(self, other: Outcome) -> Outcome:
        self.grants += other.grants
        self.released += other.released
        self.timed_out += other.timed_out
        self.dropped += other.dropped
        return self


class Refused(Exception):
    """A release by someone who isn't the holder."""

    def __init__(self, message: str, leases: list[Lease]):
        super().__init__(message)
        self.leases = leases


class Broker:
    def __init__(self, resources: dict[str, Resource], *, clock: Callable[[], float] = time.time,
                 alive: Callable[[int | None, str | None], bool] = proc.alive):
        self.resources: dict[str, Resource] = dict(resources)
        self.clock = clock
        self.alive = alive
        self.leases: list[Lease] = []
        self.waiters: list[Waiter] = []
        self.seq = 0
        self.events: deque[dict] = deque(maxlen=500)
        self.new_events: list[dict] = []

    # ---- events --------------------------------------------------------------------------------

    def emit(self, kind: str, **data):
        ev = {"ts": self.clock(), "kind": kind, **data}
        self.events.append(ev)
        self.new_events.append(ev)

    def take_events(self) -> list[dict]:
        out, self.new_events = self.new_events, []
        return out

    # ---- resources -----------------------------------------------------------------------------

    def ensure(self, res: Resource):
        """Make a family lock known on first use."""
        self.resources.setdefault(res.name, res)

    def update_resources(self, resources: dict[str, Resource]) -> Outcome:
        """A config reload: new sizes apply at once. A resource that's gone stays while anyone holds or awaits it."""
        busy = {l.resource for l in self.leases} | {r for w in self.waiters for r in w.resources}
        keep = {n: r for n, r in self.resources.items() if r.family or (n in busy and n not in resources)}
        self.resources = {**keep, **resources}
        self.emit("config", resources=sorted(resources))
        return self.schedule()

    def held(self, name: str) -> list[Lease]:
        return [l for l in self.leases if l.resource == name]

    # ---- acquiring -----------------------------------------------------------------------------

    def request(self, names: list[str], owner: Owner, *, token: str | None = None, conn=None,
                wait: bool = False, timeout: float | None = None, idempotent: bool = False
                ) -> tuple[str, Waiter | None, list[Lease], Outcome]:
        """Ask for one unit of each named resource, all at once.

        Returns (status, waiter, leases, outcome) where status is "granted", "already" (the owner holds
        them all; only with idempotent), "busy" (not granted and not waiting), or "queued".
        """
        for n in names:
            if n not in self.resources:
                raise KeyError(n)
        if len(set(names)) != len(names):
            raise ValueError("a resource is named twice")
        token = token or secrets.token_hex(8)

        # A client reconnecting after a daemon restart picks up its place in the queue, or the leases
        # it was granted while it was away.
        if any(l.token == token and l.kind == "conn" and l.orphan_until for l in self.leases):
            return "granted", None, self.reclaim(token, conn), Outcome()
        for w in self.waiters:
            if w.token == token:
                w.conn, w.orphan_until, w.position = conn, None, None
                out = self.schedule()
                return ("granted" if self._granted_in(w, out) else "queued"), w, self._leases_of(w, out), out

        already: list[Lease] = []
        if idempotent:
            for n in names:
                mine = [l for l in self.held(n) if l.kind != "conn" and l.owner.same_holder(owner)]
                if mine:
                    already.append(mine[0])
            names = [n for n in names if n not in {l.resource for l in already}]
            if not names:
                return "already", None, already, Outcome()

        self.seq += 1
        now = self.clock()
        w = Waiter(token, self.seq, list(names), owner, now, now + timeout if timeout else None, conn)
        self.waiters.append(w)
        out = self.schedule()
        if self._granted_in(w, out):
            return "granted", w, already + self._leases_of(w, out), out
        if not wait:
            self.waiters.remove(w)
            # Removing a waiter can unblock the ones it was reserving for.
            out.merge(self.schedule())
            return "busy", w, already, out
        self.emit("queued", lane=owner.lane, label=owner.label, resources=names)
        return "queued", w, already, out

    @staticmethod
    def _granted_in(w: Waiter, out: Outcome) -> bool:
        return any(g.waiter is w for g in out.grants)

    @staticmethod
    def _leases_of(w: Waiter, out: Outcome) -> list[Lease]:
        return [l for g in out.grants if g.waiter is w for l in g.leases]

    def schedule(self) -> Outcome:
        out = Outcome()
        free = {n: r.size - len(self.held(n)) for n, r in self.resources.items()}
        for w in sorted(self.waiters, key=lambda w: w.seq):
            if all(free.get(n, 0) > 0 for n in w.resources):
                leases = [self._grant(n, w) for n in w.resources]
                for n in w.resources:
                    free[n] -= 1
                self.waiters.remove(w)
                out.grants.append(Grant(w, leases))
                self.emit("granted", lane=w.owner.lane, label=w.owner.label,
                          resources=[f"{l.resource}#{l.unit}" for l in leases], waited=round(self.clock() - w.since, 1))
            else:
                for n in w.resources:
                    free[n] = free.get(n, 0) - 1
        return out

    def _grant(self, name: str, w: Waiter) -> Lease:
        taken = {l.unit for l in self.held(name)}
        unit = next(u for u in range(1, len(taken) + 2) if u not in taken)
        now = self.clock()
        # A waiter granted while its client is reconnecting keeps the same grace to reclaim the lease.
        lease = Lease(secrets.token_hex(6), name, unit, "conn", w.owner, w.token, now, now, w.conn, w.orphan_until)
        self.leases.append(lease)
        return lease

    def detach(self, token: str, kind: str, owner: Owner | None = None) -> list[Lease]:
        """Turn a connection's fresh leases into leases that outlive it ("pid" or "manual")."""
        if kind not in ("pid", "manual"):
            raise ValueError(kind)
        mine = [l for l in self.leases if l.token == token and l.kind == "conn"]
        for l in mine:
            l.kind, l.conn = kind, None
            if owner is not None:
                l.owner = owner
            if kind == "pid" and not l.owner.pid:
                raise ValueError("a pid lease needs a pid")
        return mine

    def reclaim(self, token: str, conn) -> list[Lease]:
        """A client reconnecting after a daemon restart takes back its connection-held leases."""
        mine = [l for l in self.leases if l.token == token and l.kind == "conn"]
        for l in mine:
            l.conn, l.orphan_until = conn, None
        return mine

    # ---- releasing -----------------------------------------------------------------------------

    def release(self, names: list[str] | None, owner: Owner, *, admin: bool = False,
                unit: int | None = None, lease_id: str | None = None) -> tuple[list[Lease], Outcome]:
        """Release the requester's leases on `names` (all its leases when None).

        Raises Refused when the requester holds none of a named resource and someone else does. Returns
        ([], ...) when there was nothing to release. An admin release takes any lease that matches.
        """
        targets: list[Lease] = []
        for l in self.leases:
            if names is not None and l.resource not in names:
                continue
            if unit is not None and l.unit != unit:
                continue
            if lease_id is not None and l.id != lease_id:
                continue
            if admin or l.owner.same_holder(owner):
                targets.append(l)
        if not targets and names is not None and not admin:
            others = [l for l in self.leases if l.resource in names]
            if others:
                who = "; ".join(f"{l.owner.describe()} ({l.resource} #{l.unit})" for l in others)
                raise Refused(f"not yours: held by {who}", others)
        return targets, self._end(targets, "admin-release" if admin else "released")

    def release_token(self, token: str) -> Outcome:
        return self._end([l for l in self.leases if l.token == token], "released")

    def _end(self, leases: list[Lease], why: str) -> Outcome:
        for l in leases:
            self.leases.remove(l)
            self.emit(why, lane=l.owner.lane, label=l.owner.label, resource=l.resource, unit=l.unit,
                      held=round(self.clock() - l.since, 1))
        out = self.schedule()
        out.released += leases
        return out

    def dequeue(self, waiter: Waiter, why: str = "dequeued") -> Outcome:
        if waiter in self.waiters:
            self.waiters.remove(waiter)
            self.emit(why, lane=waiter.owner.lane, label=waiter.owner.label, resources=waiter.resources)
        out = self.schedule()
        out.dropped.append(waiter)
        return out

    def drop_conn(self, conn) -> Outcome:
        """A client's connection closed: its waits end and its connection-held leases are released."""
        out = Outcome()
        for w in [w for w in self.waiters if w.conn is conn]:
            out.merge(self.dequeue(w, "gave-up"))
        out.merge(self._end([l for l in self.leases if l.kind == "conn" and l.conn is conn], "released"))
        return out

    # ---- time and liveness ---------------------------------------------------------------------

    def orphan_all(self):
        """After a restart: every connection-held lease and waiter has RECLAIM_GRACE to be reclaimed."""
        until = self.clock() + RECLAIM_GRACE
        for l in self.leases:
            if l.kind == "conn":
                l.conn, l.orphan_until = None, until
        for w in self.waiters:
            w.conn, w.orphan_until = None, until

    def tick(self) -> Outcome:
        """Waiter timeouts and unclaimed orphans. Cheap; called often."""
        now = self.clock()
        out = Outcome()
        for w in list(self.waiters):
            if w.deadline is not None and now >= w.deadline:
                out.merge(self.dequeue(w, "timed-out"))
                out.timed_out.append(w)
            elif w.orphan_until is not None and now >= w.orphan_until:
                out.merge(self.dequeue(w, "gave-up"))
        lost = [l for l in self.leases if l.orphan_until is not None and now >= l.orphan_until]
        if lost:
            out.merge(self._end(lost, "lost"))
        return out

    def sweep(self) -> Outcome:
        """Dead pids and opt-in ttls. Checks every pid lease; called every couple of seconds."""
        now = self.clock()
        dead, expired = [], []
        for l in self.leases:
            if l.kind == "pid" and not self.alive(l.owner.pid, l.owner.pid_start):
                dead.append(l)
            elif l.kind != "conn":
                ttl = self.resources.get(l.resource) and self.resources[l.resource].ttl
                if ttl and now - l.last_active > ttl:
                    expired.append(l)
        out = Outcome()
        if dead:
            out.merge(self._end(dead, "holder-died"))
        if expired:
            out.merge(self._end(expired, "expired"))
        return out

    def touch(self, owner: Owner) -> int:
        """Activity from an owner (a hook heartbeat): refreshes last_active on its leases."""
        now, n = self.clock(), 0
        for l in self.leases:
            if l.owner.same_holder(owner):
                l.last_active, n = now, n + 1
        return n

    # ---- views ---------------------------------------------------------------------------------

    def position(self, w: Waiter) -> int:
        """1 + the number of earlier waiters wanting any of the same resources."""
        want = set(w.resources)
        return 1 + sum(1 for o in self.waiters if o.seq < w.seq and want & set(o.resources))

    def blockers(self, w: Waiter) -> list[Lease]:
        return [l for l in self.leases if l.resource in w.resources]

    def snapshot(self) -> dict:
        now = self.clock()
        res = []
        for name, r in sorted(self.resources.items()):
            held = sorted(self.held(name), key=lambda l: l.unit)
            queue = sorted((w for w in self.waiters if name in w.resources), key=lambda w: w.seq)
            if r.family and not held and not queue:
                continue
            res.append({
                "name": name, "kind": r.kind, "size": r.size, "hold": r.hold, "family": r.family,
                "ttl": r.ttl, "in_use": len(held),
                "holders": [{**l.row(), "age": round(now - l.since, 1),
                             "idle": round(now - l.last_active, 1)} for l in held],
                "queue": [{"token": w.token, "seq": w.seq, "owner": asdict(w.owner), "resources": w.resources,
                           "since": w.since, "waited": round(now - w.since, 1), "deadline": w.deadline,
                           "orphan": w.orphan_until is not None} for w in queue],
            })
        return {"now": now, "resources": res, "events": list(self.events)[-50:]}

    # ---- persistence ---------------------------------------------------------------------------

    def rows(self) -> dict:
        return {"seq": self.seq, "leases": [l.row() for l in self.leases], "waiters": [w.row() for w in self.waiters]}

    def load_rows(self, rows: dict, resolve: Callable[[str], Resource | None]):
        self.seq = rows.get("seq", 0)
        for d in rows.get("leases", []):
            res = self.resources.get(d["resource"]) or resolve(d["resource"])
            if res:
                self.ensure(res)
            self.leases.append(Lease(**{**d, "owner": Owner.of(d["owner"])}))
        for d in rows.get("waiters", []):
            if all(n in self.resources or resolve(n) for n in d["resources"]):
                for n in d["resources"]:
                    r = resolve(n)
                    if r:
                        self.ensure(r)
                self.waiters.append(Waiter(**{**d, "owner": Owner.of(d["owner"])}))
        self.orphan_all()
