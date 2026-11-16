"""Config: one TOML file declaring lanes and resources.

Found via --config, then $SLOTS_CONFIG, then ./.slots.toml (searched upward from
the working directory), then ~/.config/slots/config.toml.
"""
from __future__ import annotations

import fnmatch
import os
import re
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

USER_CONFIG = Path("~/.config/slots/config.toml").expanduser()
PROJECT_NAME = ".slots.toml"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Resource:
    name: str
    kind: str                     # "pool" or "lock"
    size: int = 1
    hold: str = "command"         # "command" or "session": informational, used by hooks and the skill
    ttl: float | None = None      # opt-in expiry of inactive non-command leases
    family: str | None = None     # set on locks created from a family


@dataclass(frozen=True)
class Family:
    name: str
    template: str                 # e.g. "next-employee-{key}"
    key: str = "sha1-12"          # "sha1-12" or "raw"
    normalize: tuple[str, ...] = ("trim", "lower")

    def lock_name(self, value: str) -> str:
        for step in self.normalize:
            value = value.strip() if step == "trim" else value.lower() if step == "lower" else value
        if self.key == "sha1-12":
            import hashlib
            value = hashlib.sha1(value.encode()).hexdigest()[:12]
        return self.template.format(key=value)

    def matches(self, name: str) -> bool:
        prefix, _, suffix = self.template.partition("{key}")
        key = r"[0-9a-f]{12}" if self.key == "sha1-12" else r".+"
        return re.fullmatch(re.escape(prefix) + key + re.escape(suffix), name) is not None


@dataclass
class Config:
    path: Path | None
    lanes_root: Path | None = None
    lane_pattern: re.Pattern = field(default_factory=lambda: re.compile(r"^(?P<ticket>.+)$"))
    resources: dict[str, Resource] = field(default_factory=dict)
    families: dict[str, Family] = field(default_factory=dict)
    device: str | None = None
    raw: dict = field(default_factory=dict)

    def resolve(self, spec: str) -> Resource:
        """A resource by name: a pool or lock, `<family>:<value>`, or a family lock's own name."""
        if spec in self.resources:
            return self.resources[spec]
        fam_name, sep, value = spec.partition(":")
        if sep and fam_name in self.families:
            if not value.strip():
                raise ConfigError(f"{spec!r}: give a key after {fam_name}:")
            return Resource(self.families[fam_name].lock_name(value), "lock", family=fam_name)
        for fam in self.families.values():
            if fam.matches(spec):
                return Resource(spec, "lock", family=fam.name)
        known = sorted(self.resources) + [f"{f}:<key>" for f in sorted(self.families)]
        raise ConfigError(f"unknown resource {spec!r} (known: {', '.join(known) or 'none'})")

    def lane_of(self, cwd: str | Path) -> str | None:
        """The lane (ticket id) of the folder under lanes.root that contains cwd."""
        if self.lanes_root is None:
            return None
        try:
            rel = Path(os.path.realpath(cwd)).relative_to(os.path.realpath(self.lanes_root))
        except ValueError:
            return None
        if not rel.parts:
            return None
        m = self.lane_pattern.search(rel.parts[0])
        if not m:
            return None
        return m.groupdict().get("ticket") or m.group(0)


def parse_duration(value) -> float | None:
    """30, "30s", "10m", "2h", "1h30m" -> seconds; None/"" -> None."""
    if value is None or value == "" or value is False:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    total, rest = 0.0, str(value).strip()
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([smhd]?)", rest):
        total += float(num) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if not re.fullmatch(r"(\s*\d+(?:\.\d+)?\s*[smhd]?)+\s*", rest):
        raise ConfigError(f"bad duration {value!r} (use e.g. 30s, 10m, 2h)")
    return total


def find_config(explicit: str | None = None, cwd: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("SLOTS_CONFIG")
    if env:
        return Path(env).expanduser()
    here = Path(cwd or os.getcwd()).resolve()
    for d in (here, *here.parents):
        if (d / PROJECT_NAME).is_file():
            return d / PROJECT_NAME
    if USER_CONFIG.is_file():
        return USER_CONFIG
    return None


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _device(raw: dict) -> tuple[str | None, dict]:
    """The [device.<name>] override for this machine: $SLOTS_DEVICE, else the first `match` glob on the hostname."""
    devices = raw.get("device", {})
    forced = os.environ.get("SLOTS_DEVICE")
    if forced:
        if forced not in devices:
            raise ConfigError(f"SLOTS_DEVICE={forced} but there is no [device.{forced}]")
        return forced, devices[forced]
    host = socket.gethostname()
    names = {host, host.split(".")[0]}
    for name, dev in devices.items():
        pattern = dev.get("match", name)
        if any(fnmatch.fnmatch(n, pattern) for n in names):
            return name, dev
    return None, {}


def load(path: Path | None) -> Config:
    if path is None:
        raise ConfigError("no config found: run `slots init` (or pass --config / set SLOTS_CONFIG)")
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return from_dict(raw, path)


def from_dict(raw: dict, path: Path | None = None) -> Config:
    device, override = _device(raw)
    merged = _merge(raw, {k: v for k, v in override.items() if k != "match"})
    cfg = Config(path=path, device=device, raw=merged)

    lanes = merged.get("lanes", {})
    if "root" in lanes:
        cfg.lanes_root = Path(lanes["root"]).expanduser()
    if "pattern" in lanes:
        try:
            cfg.lane_pattern = re.compile(lanes["pattern"])
        except re.error as e:
            raise ConfigError(f"lanes.pattern: {e}") from None

    def add(res: Resource):
        if res.name in cfg.resources or res.name in cfg.families:
            raise ConfigError(f"resource {res.name!r} is declared twice")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", res.name):
            raise ConfigError(f"bad resource name {res.name!r} (letters, digits, . _ -)")
        cfg.resources[res.name] = res

    for name, spec in merged.get("pool", {}).items():
        size = spec.get("size")
        if not isinstance(size, int) or size < 1:
            raise ConfigError(f"pool.{name}.size must be a whole number of at least 1")
        add(Resource(name, "pool", size, _hold(name, spec), parse_duration(spec.get("ttl"))))
    for name, spec in merged.get("lock", {}).items():
        if spec.get("queue", "fifo") != "fifo":
            raise ConfigError(f"lock.{name}.queue: only \"fifo\" is supported")
        add(Resource(name, "lock", 1, _hold(name, spec), parse_duration(spec.get("ttl"))))
    for name, spec in merged.get("family", {}).items():
        if name in cfg.resources:
            raise ConfigError(f"resource {name!r} is declared twice")
        key = spec.get("key", "sha1-12")
        if key not in ("sha1-12", "raw"):
            raise ConfigError(f"family.{name}.key must be \"sha1-12\" or \"raw\"")
        template = spec.get("name", name + "-{key}")
        if "{key}" not in template:
            raise ConfigError(f"family.{name}.name must contain {{key}}")
        cfg.families[name] = Family(name, template, key, tuple(spec.get("normalize", ("trim", "lower"))))

    if merged.get("cluster", {}).get("enabled"):
        raise ConfigError("cluster mode is not built yet (set cluster.enabled = false)")
    return cfg


def _hold(name: str, spec: dict) -> str:
    hold = spec.get("hold", "command")
    if hold not in ("command", "session"):
        raise ConfigError(f"{name}.hold must be \"command\" or \"session\"")
    return hold
