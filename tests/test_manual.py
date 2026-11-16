"""The manual stays complete and in step with the code."""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from slots import cli, config, manual
from tests.helpers import SRC

ROOT = Path(__file__).resolve().parent.parent


def run(*args):
    return subprocess.run([sys.executable, "-m", "slots", *args], capture_output=True, text=True,
                          env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()})


class ManualTest(unittest.TestCase):
    def test_topics_match_the_index_and_exist(self):
        listed = [t for t, _ in manual.topics()]
        self.assertEqual(sorted(listed), sorted(manual.TOPICS))
        for t in ["index", *manual.TOPICS]:
            self.assertTrue(manual.page(t).startswith("# "), t)

    def test_every_command_is_documented(self):
        sub = next(a for a in cli.parser()._actions if a.dest == "cmd")
        cli_page = manual.page("cli")
        for name in sub.choices:
            with self.subTest(name):
                self.assertRegex(cli_page, rf"(?m)^## slots {name}\b", f"cli.md has no section for `slots {name}`")

    def test_every_option_is_documented(self):
        sub = next(a for a in cli.parser()._actions if a.dest == "cmd")
        cli_page = manual.page("cli")
        for name, p in sub.choices.items():
            if name == "compat":
                continue
            section = manual.command_section(name) or ""
            for action in p._actions:
                for opt in action.option_strings:
                    if opt.startswith("--") and opt not in ("--help", "--config"):
                        with self.subTest(command=name, option=opt):
                            self.assertIn(opt, section, f"`slots {name} {opt}` isn't in cli.md")

    def test_every_config_table_and_key_is_documented(self):
        page = manual.page("config")
        for table in ("lanes", "pool.NAME", "lock.NAME", "family.NAME", "device.NAME", "warn", "protect",
                      "compat", "cluster"):
            self.assertIn(f"## [{table}]", page)
        for key in [*config.WARN_DEFAULTS, *config.COMPAT_DEFAULTS, "root", "pattern", "processes", "size",
                    "hold", "ttl", "name", "key", "normalize", "match", "containers", "ports"]:
            self.assertIn(f"`{key}`", page, key)

    def test_every_environment_variable_is_documented(self):
        used = set()
        for f in (ROOT / "src" / "slots").glob("*.py"):
            used |= set(re.findall(r"environ(?:\.get)?\(\s*\"(SLOTS_[A-Z_]+)\"", f.read_text()))
            used |= set(re.findall(r"environ\[\"(SLOTS_[A-Z_]+)\"\]", f.read_text()))
        page = manual.page("cli")
        for var in used:
            self.assertIn(f"`{var}`", page, var)

    def test_links_between_pages_resolve(self):
        for t in ["index", *manual.TOPICS]:
            for target in re.findall(r"\]\((\w+)\.md\)", manual.page(t)):
                self.assertIn(target, ["index", *manual.TOPICS], f"{t}.md links to {target}.md")

    def test_help_command(self):
        r = run("help")
        self.assertEqual(r.returncode, 0)
        for t in manual.TOPICS:
            self.assertIn(t, r.stdout)
        self.assertIn("all.txt", r.stdout)
        r = run("help", "agents")
        self.assertIn("Hold an agent slot before you start", r.stdout)
        r = run("help", "acquire")
        self.assertTrue(r.stdout.startswith("## slots acquire"))
        self.assertEqual(run("help", "nope").returncode, 2)

    def test_man_page(self):
        r = run("man")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith(".TH SLOTS 1 "))
        self.assertTrue(r.stdout.isascii(), "non-ASCII must be escaped for roff")
        for section in ("NAME", "SYNOPSIS", "DESCRIPTION", "QUICK START", "FOR AGENTS", "COMMANDS", "CONFIG"):
            self.assertIn(f'.SH "{section}"' if " " in section or section not in ("NAME", "SYNOPSIS", "DESCRIPTION")
                          else f".SH {section}", r.stdout)
        renderer = shutil.which("mandoc") or shutil.which("groff")
        if renderer:
            cmd = [renderer, "-Tutf8", "-W", "warning"] if renderer.endswith("mandoc") else \
                [renderer, "-man", "-Tutf8", "-ww"]
            out = subprocess.run(cmd, input=r.stdout, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, f"{renderer}:\n{out.stderr[:3000]}")
            self.assertEqual(out.stderr.strip(), "", "the man page renders without warnings")
            self.assertIn("slots run", out.stdout)

    def test_man_install(self):
        with tempfile.TemporaryDirectory() as d:
            r = run("man", "--install", "--dir", d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((Path(d) / "man1" / "slots.1").read_text().startswith(".TH"))

    def test_site(self):
        with tempfile.TemporaryDirectory() as d:
            manual.site(Path(d) / "wiki")
            for t in ["index", *manual.TOPICS]:
                text = (Path(d) / "wiki" / f"{t}.md").read_text()
                self.assertTrue(text.startswith("---\ntitle: "), t)
            all_txt = (Path(d) / "wiki" / "all.txt").read_text()
            for t in manual.TOPICS:
                self.assertIn(manual.page(t).splitlines()[0], all_txt)
            self.assertIn("wiki/agents.html", (Path(d) / "llms.txt").read_text())

    def test_manual_ships_in_the_package(self):
        from importlib import resources
        names = {p.name for p in resources.files("slots").joinpath("manual").iterdir()}
        self.assertTrue({f"{t}.md" for t in ["index", *manual.TOPICS]} <= names)
        self.assertIn('"manual/*.md"', (ROOT / "pyproject.toml").read_text())


if __name__ == "__main__":
    unittest.main()
