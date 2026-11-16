"""Small, careful edits to the user's config file: only a pool's `size` line, in place.

The file is the user's: comments, order and formatting are kept, nothing else is
rewritten, and the result is parsed before it replaces the original (atomically).
"""
from __future__ import annotations

import os
import re
import tempfile
import tomllib
from pathlib import Path

from slots import config

TABLE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(#.*)?$")


def _size_line(lines: list[str], table: str, key: str) -> int | None:
    """The index of `key = <int>` inside [table], or None."""
    inside = False
    for i, line in enumerate(lines):
        m = TABLE.match(line)
        if m:
            inside = m.group(1).replace('"', "").replace(" ", "") == table
            continue
        if inside and re.match(rf"^\s*{re.escape(key)}\s*=\s*\d+", line):
            return i
    return None


def set_pool_size(path: Path, pool: str, size: int, device: str | None = None) -> str:
    """Set [pool.<pool>] size (or this device's override of it, when it has one). Returns what changed."""
    if size < 1:
        raise config.ConfigError("a pool needs at least 1 slot")
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    where = None
    if device:
        i = _size_line(lines, f"device.{device}", f"pool.{pool}.size")
        if i is not None:
            where = (i, f"[device.{device}] pool.{pool}.size")
    if where is None:
        i = _size_line(lines, f"pool.{pool}", "size")
        if i is None:
            raise config.ConfigError(f"no `size = N` line under [pool.{pool}] in {path}")
        where = (i, f"[pool.{pool}] size")
    i, label = where
    old = re.search(r"=\s*(\d+)", lines[i]).group(1)
    lines[i] = re.sub(r"(=\s*)\d+", lambda m: f"{m.group(1)}{size}", lines[i], count=1)
    new_text = "".join(lines)
    tomllib.loads(new_text)                   # never write a file that doesn't parse
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return f"{label}: {old} -> {size}"
