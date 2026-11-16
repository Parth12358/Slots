"""Port leases: a stable port per lane and service, never one another lane holds or one in use.

The first choice is a hash of "<lane>/<service>" into the configured range, so a lane gets the
same port every time even if its lease is released; from there the search walks forward past
ports that are held, protected, or already listening.
"""
from __future__ import annotations

import errno
import hashlib
import re
import socket


def first_choice(lane: str, service: str, low: int, high: int) -> int:
    h = int(hashlib.sha1(f"{lane}/{service}".encode()).hexdigest()[:8], 16)
    return low + h % (high - low + 1)


def in_use(port: int) -> bool:
    """Something listens on this port (any address), as far as a bind can tell."""
    for family, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::")):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            s.bind((addr, port))
        except OSError as e:
            if e.errno in (errno.EADDRINUSE, errno.EACCES):
                return True
        finally:
            s.close()
    return False


def candidates(lane: str, service: str, low: int, high: int, protected: set[int]):
    start = first_choice(lane, service, low, high)
    span = high - low + 1
    for i in range(span):
        port = low + (start - low + i) % span
        if port not in protected and not in_use(port):
            yield port


def env_name(template: str, service: str) -> str:
    name = template.replace("{SERVICE}", service.upper()).replace("{service}", service)
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


SERVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}")
