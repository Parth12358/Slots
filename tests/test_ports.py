"""Port leases: stable per lane and service, never another lane's, never in use or protected."""
import os
import signal
import socket
import tomllib
import unittest

from slots import config, dashboard, ports
from tests.helpers import World


class PortTest(World):
    extra_config = """
    [ports]
    range = [41000, 41019]
    env = "{SERVICE}_PORT"

    [protect]
    ports = [41005]
    """

    def port(self, *args, check=0):
        return self.slots("port", *args, check=check)

    def test_stable_unique_and_refused(self):
        ui = int(self.port("10526", "ui").stdout)
        self.assertTrue(41000 <= ui <= 41019)
        self.assertEqual(int(self.port("10526", "ui").stdout), ui, "asking again gives the same port")
        api = int(self.port("10526", "api").stdout)
        other = int(self.port("11852", "ui").stdout)
        self.assertEqual(len({ui, api, other}), 3)
        r = self.port("11852", "web", "--want", str(ui), check=1)
        self.assertIn(f"refused: port {ui} is held by lane 10526 (ui)", r.stderr)
        r = self.port("11852", "web", "--want", "41005", check=1)
        self.assertIn("protected", r.stderr)

    def test_env_fragment_release_and_same_port_again(self):
        ui = self.port("10526", "ui").stdout.strip()
        api = self.port("10526", "api").stdout.strip()
        self.assertEqual(self.port("10526", "ui", "--env").stdout, f"UI_PORT={ui}\n")
        self.assertEqual(self.port("10526", "--env").stdout, f"API_PORT={api}\nUI_PORT={ui}\n")
        self.assertIn("released", self.port("10526", "--release").stdout)
        self.assertEqual(self.port("10526", "--env").stdout, "")
        self.assertEqual(self.port("10526", "ui").stdout.strip(), ui, "the first choice is a hash: stable across releases")

    def test_skips_ports_in_use_and_protected(self):
        first = ports.first_choice("10526", "ui", 41000, 41019)
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        blocker.bind(("127.0.0.1", first))
        blocker.listen()
        try:
            got = int(self.port("10526", "ui").stdout)
        finally:
            blocker.close()
        self.assertNotEqual(got, first)
        self.assertNotEqual(got, 41005)

    def test_range_runs_out(self):
        got = {int(self.port("1", f"s{i}").stdout) for i in range(19)}   # 20 ports, one protected
        self.assertEqual(len(got), 19)
        self.assertIn("no free port left", self.port("1", "one-more", check=1).stderr)

    def test_survives_a_daemon_restart(self):
        ui = self.port("10526", "ui").stdout
        os.kill(self.daemon_pid(), signal.SIGKILL)
        self.assertEqual(self.port("10526", "ui").stdout, ui)
        self.assertEqual(len(self.state()["ports"]), 1)

    def test_bad_input(self):
        self.port("10526", "bad name!", check=2)
        self.slots("port", "10526", "--want", "41001", check=2)

    def test_status_shows_leases(self):
        ui = self.port("10526", "ui").stdout.strip()
        out = self.slots("status", check=0).stdout
        self.assertRegex(out, rf":{ui}\s+10526\s+ui\s+not up")


class ConflictTest(unittest.TestCase):
    def test_another_lane_listening_on_a_leased_port_needs_you(self):
        cfg = config.from_dict(tomllib.loads('[lanes]\nroot="/l"\npattern=\'^(?P<ticket>\\d+)$\'\n'))
        state = {"now": 1.0, "events": [], "resources": [], "sessions": [],
                 "ports": [{"lane": "10526", "service": "ui", "port": 4352, "since": 0}]}
        procs = {7: {"pid": 7, "ppid": 1, "cpu": 0, "rss": 1, "etime": "1", "secs": 1, "cmd": "node /l/11852/vite.js"}}
        mach = {"procs": procs, "ports": [{"cmd": "node", "pid": 7, "port": 4352}], "containers": [], "cwds": {}}
        needs = dashboard.build(state, mach, cfg, now=1.0)["needs"]
        self.assertEqual(len(needs), 1)
        self.assertIn("port 4352 is leased to lane 10526 (ui) but lane 11852 is listening on it", needs[0]["text"])
        procs[7]["cmd"] = "node /l/10526/vite.js"
        self.assertEqual(dashboard.build(state, mach, cfg, now=1.0)["needs"], [])

    def test_env_names(self):
        self.assertEqual(ports.env_name("{SERVICE}_PORT", "ui"), "UI_PORT")
        self.assertEqual(ports.env_name("PORT_{service}", "api-gw"), "PORT_api_gw")


if __name__ == "__main__":
    unittest.main()
