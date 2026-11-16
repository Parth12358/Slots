import hashlib
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from slots import config

REFERENCE = Path(__file__).resolve().parent.parent / "reference"


def cfg(text: str, **env) -> config.Config:
    import tomllib
    with mock.patch.dict(os.environ, env):
        return config.from_dict(tomllib.loads(text))


BASE = """
[lanes]
root = "/work/lanes"
pattern = '^(?P<ticket>\\d+)$'
[pool.agent]
size = 7
hold = "session"
[pool.test]
size = 5
[lock.e2e]
[family.next-employee]
name = "next-employee-{key}"
"""


class ConfigTest(unittest.TestCase):
    def test_resources(self):
        c = cfg(BASE)
        self.assertEqual(c.resources["agent"].size, 7)
        self.assertEqual(c.resources["agent"].hold, "session")
        self.assertEqual(c.resources["test"].hold, "command")
        self.assertEqual(c.resources["e2e"].kind, "lock")
        self.assertIsNone(c.resources["agent"].ttl, "no inactivity expiry unless asked for")

    def test_family_lock_name_matches_reference_env_ts(self):
        # lib/env.ts: sha1(email.trim().toLowerCase()).hex.slice(0, 12)
        c = cfg(BASE)
        email = "  Test.Employee@Example.com "
        want = "next-employee-" + hashlib.sha1(email.strip().lower().encode()).hexdigest()[:12]
        self.assertEqual(c.resolve("next-employee:" + email).name, want)
        self.assertEqual(c.resolve(want).name, want, "a family lock's own name resolves too")
        self.assertEqual(c.resolve(want).family, "next-employee")

    @unittest.skipUnless((REFERENCE / "lib-slot.sh").exists() and shutil.which("bash")
                         and (shutil.which("shasum") or shutil.which("sha1sum")), "needs reference/ and shasum")
    def test_family_lock_name_matches_reference_lib_slot_sh(self):
        email = " Someone@Example.COM"
        shasum = "shasum -a 1" if shutil.which("shasum") else "sha1sum"
        script = (REFERENCE / "lib-slot.sh").read_text().replace("shasum -a 1", shasum)
        out = subprocess.run(["bash", "-c", script + '\nnext_employee_lock "$1"', "_", email],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(cfg(BASE).resolve("next-employee:" + email).name, out)

    def test_unknown_resource(self):
        with self.assertRaisesRegex(config.ConfigError, "unknown resource 'nope'.*agent"):
            cfg(BASE).resolve("nope")

    def test_lane_of(self):
        c = cfg(BASE)
        self.assertEqual(c.lane_of("/work/lanes/301"), "301")
        self.assertEqual(c.lane_of("/work/lanes/301/services/api"), "301")
        self.assertIsNone(c.lane_of("/work/lanes"))
        self.assertIsNone(c.lane_of("/work/lanes/ta-e2e"))
        self.assertIsNone(c.lane_of("/elsewhere/301"))

    def test_device_override_by_hostname(self):
        text = BASE + '[device.desktop]\nmatch = "big-*"\npool.agent.size = 12\n'
        with mock.patch("socket.gethostname", return_value="big-box.local"):
            c = cfg(text)
        self.assertEqual(c.device, "desktop")
        self.assertEqual(c.resources["agent"].size, 12)
        self.assertEqual(c.resources["agent"].hold, "session", "override merges, doesn't replace")
        with mock.patch("socket.gethostname", return_value="laptop"):
            self.assertEqual(cfg(text).resources["agent"].size, 7)
        with mock.patch("socket.gethostname", return_value="laptop"):
            self.assertEqual(cfg(text, SLOTS_DEVICE="desktop").resources["agent"].size, 12)

    def test_durations(self):
        self.assertEqual(config.parse_duration("90"), 90)
        self.assertEqual(config.parse_duration("1h30m"), 5400)
        self.assertEqual(config.parse_duration(30), 30)
        self.assertIsNone(config.parse_duration(None))
        with self.assertRaises(config.ConfigError):
            config.parse_duration("soon")
        self.assertEqual(cfg(BASE.replace('hold = "session"', 'hold = "session"\nttl = "2h"')).resources["agent"].ttl, 7200)

    def test_errors(self):
        for bad, msg in [("[pool.x]\nsize = 0", "at least 1"), ("[pool.a]\nsize=1\n[lock.a]", "twice"),
                         ("[lock.e]\nqueue = 'lifo'", "fifo"), ("[cluster]\nenabled = true", "not built yet"),
                         ("[family.f]\nname = 'f'", "{key}")]:
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError, msg):
                cfg(bad)

    def test_find_config_order(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d, "a", "b")
            sub.mkdir(parents=True)
            Path(d, ".slots.toml").write_text("")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SLOTS_CONFIG", None)
                self.assertEqual(config.find_config(cwd=str(sub)), Path(d, ".slots.toml").resolve())
                os.environ["SLOTS_CONFIG"] = "/x.toml"
                self.assertEqual(config.find_config(cwd=str(sub)), Path("/x.toml"))
                self.assertEqual(config.find_config("/y.toml", cwd=str(sub)), Path("/y.toml"))

    def test_shipped_examples_load(self):
        root = Path(__file__).resolve().parent.parent
        for p in [root / "src/slots/example.toml", *root.glob("examples/*.toml")]:
            with self.subTest(p.name):
                config.load(p)


if __name__ == "__main__":
    unittest.main()
