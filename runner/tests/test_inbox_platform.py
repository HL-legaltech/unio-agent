"""The cross-platform layer: names, locks, process groups, text, zones, and
the cleanup of what Claude Code keeps per session folder."""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from inbox_support import HomeCase, inbox


class PlatformName(unittest.TestCase):
    def test_names(self):
        self.assertEqual(inbox.platform_name("linux"), "linux")
        self.assertEqual(inbox.platform_name("darwin"), "macos")
        self.assertEqual(inbox.platform_name("win32"), "windows")

    def test_memory_guard_is_linux_only(self):
        with mock.patch.object(inbox.sys, "platform", "darwin"), \
                mock.patch.object(inbox, "MEMINFO", "/nonexistent"):
            self.assertIsNone(inbox.desk_memory())
        with mock.patch.object(inbox.sys, "platform", "win32"):
            self.assertIsNone(inbox.desk_memory())


# A typical small server: 4 GiB, 3 available, no swap at all.
SMALL_MACHINE = "MemTotal:        4194304 kB\nMemFree:          524288 kB\nMemAvailable:    3145728 kB\n" \
                "SwapTotal:             0 kB\nSwapFree:              0 kB\n"


class MemoryFloors(HomeCase):
    """The floors measured on the workstation the runner without --workspace
    serves would hold a profile's answers on most other machines forever."""

    def setUp(self):
        super().setUp()
        meminfo = self.tmp / "meminfo"
        meminfo.write_text(SMALL_MACHINE, encoding="utf-8")
        for patch in (mock.patch.object(inbox.sys, "platform", "linux"),
                      mock.patch.object(inbox, "MEMINFO", str(meminfo)),
                      mock.patch.object(inbox, "DESK_FILE", self.tmp / "no-desk.json")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_workstation_floors_stay_without_a_profile(self):
        self.assertEqual(inbox.desk_memory(), "3.0 GiB free of 4 GiB")
        self.assertIsNone(inbox.Config(dict(os.environ)).memory_floor)

    def test_a_profile_has_no_floor_unless_its_env_sets_one(self):
        cfg = inbox.Config(dict(os.environ), "org_test")
        self.assertEqual((cfg.memory_floor, cfg.memory_error), (0, None))
        self.assertIsNone(inbox.desk_memory(cfg.memory_floor))
        cfg = inbox.Config(dict(os.environ, UNIO_INBOX_MEMORY_FLOOR_GIB="1.5"), "org_test")
        self.assertEqual(cfg.memory_floor, 1.5)
        self.assertIsNone(inbox.desk_memory(cfg.memory_floor))
        self.assertEqual(inbox.desk_memory(3.5), "3.0 GiB free of 4 GiB")
        cfg = inbox.Config(dict(os.environ, UNIO_INBOX_MEMORY_FLOOR_GIB="lots"), "org_test")
        self.assertEqual(cfg.memory_floor, 0)
        self.assertIn("UNIO_INBOX_MEMORY_FLOOR_GIB=lots", cfg.memory_error)

    def test_a_profile_runner_never_pauses_for_the_workstation_floors(self):
        runner = inbox.Runner(inbox.Config(dict(os.environ), "org_test"))
        runner.state.ensure()
        h = {}
        self.assertFalse(runner.memory_pause(None, h, inbox.utcnow()))
        self.assertNotIn("backoffReason", h)


class RulePaths(unittest.TestCase):
    def test_posix(self):
        self.assertEqual(inbox.rule_path("/home/u/repos/a", windows=False), "//home/u/repos/a")

    def test_windows(self):
        self.assertEqual(inbox.rule_path(r"C:\Users\u\AppData\Local\unio\repos\a", windows=True),
                         "//c/Users/u/AppData/Local/unio/repos/a")

    def test_credential_helper_quoting(self):
        self.assertEqual(inbox.shell_word("/usr/bin/gh", windows=False), "/usr/bin/gh")
        self.assertEqual(inbox.shell_word("/opt/my tools/gh", windows=False), "'/opt/my tools/gh'")
        self.assertEqual(inbox.shell_word(r"C:\Program Files\GitHub CLI\gh.exe", windows=True),
                         "'C:/Program Files/GitHub CLI/gh.exe'")


class Locks(HomeCase):
    def test_second_holder_is_refused(self):
        path = self.tmp / "lock"
        with inbox.file_lock(path) as first:
            self.assertTrue(first)
            with inbox.file_lock(path) as second:
                self.assertFalse(second)
        with inbox.file_lock(path) as again:
            self.assertTrue(again)


class Text(unittest.TestCase):
    def test_helper_output_is_utf8(self):
        p = inbox.text_run([sys.executable, "-c", "import sys; sys.stdout.buffer.write('a\\u00e7\\u00e3o\\n'.encode())"],
                           timeout=60)
        self.assertEqual(p.stdout, "a\u00e7\u00e3o\n")

    def test_bad_bytes_do_not_raise(self):
        p = inbox.text_run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff ok')"], timeout=60)
        self.assertTrue(p.stdout.endswith(" ok"))


class Trees(HomeCase):
    def test_read_only_files_go(self):
        # As git leaves its objects; on Windows only a chmod lets them go.
        tree = self.tmp / "snapshot" / ".git" / "objects" / "ab"
        tree.mkdir(parents=True)
        (tree / "cdef").write_text("x", encoding="utf-8")
        os.chmod(tree / "cdef", 0o444)
        inbox.remove_tree(self.tmp / "snapshot")
        self.assertFalse((self.tmp / "snapshot").exists())

    def test_missing_tree(self):
        inbox.remove_tree(self.tmp / "none")


class ProcessGroups(unittest.TestCase):
    def test_kill_stops_the_session_and_what_it_started(self):
        child = ("import subprocess, sys, time\n"
                 "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                 "print(p.pid, flush=True)\n"
                 "time.sleep(60)\n")
        proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True,
                                **inbox.group_flags())
        grandchild = int(proc.stdout.readline())
        proc.stdout.close()
        inbox.Runner.kill(proc, grace=5)
        self.assertIsNotNone(proc.returncode)
        if os.name != "posix":
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("the session's own child outlived it")


# Windows has no zone database of its own, and CI's Python there has no tzdata package.
HAS_ZONES = inbox._zone("UTC") is not None


class Zones(HomeCase):
    @unittest.skipUnless(HAS_ZONES, "no time zone database on this machine")
    def test_a_zone_name_is_read(self):
        cfg = inbox.Config(dict(os.environ, UNIO_REVIEW_TZ="UTC"))
        self.assertIsNone(cfg.zone_note)
        self.assertIsNone(cfg.review_error)

    def test_no_zone_database_falls_back_to_the_machine(self):
        with mock.patch.object(inbox, "_zone", return_value=None):
            cfg = inbox.Config(dict(os.environ))
        self.assertIsNotNone(cfg.review_zone)
        self.assertIn("tzdata", cfg.zone_note)
        self.assertIsNone(cfg.review_error)

    @unittest.skipUnless(HAS_ZONES, "no time zone database on this machine")
    def test_a_bad_zone_name_is_still_an_error(self):
        cfg = inbox.Config(dict(os.environ, UNIO_REVIEW="observe", UNIO_REVIEW_TZ="Nowhere/Never"))
        self.assertIn("not a time zone", cfg.review_error or "")


class ProjectCleanup(HomeCase):
    """Claude Code keeps a folder per session folder under ~/.claude/projects,
    named after the session's path with every character outside [a-zA-Z0-9]
    as '-'. The runner deletes only those of folders it made."""

    def setUp(self):
        super().setUp()
        self.projects = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects"
        self.projects.mkdir(parents=True)
        self.runs = self.tmp / "state" / "runs"
        self.work = self.runs / "comment-1" / "work"
        self.work.mkdir(parents=True)

    def slug(self, path):
        return "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in str(path))

    def make(self, name):
        d = self.projects / name
        d.mkdir()
        return d

    def test_slug_rule(self):
        self.assertEqual(inbox.project_slugs("/a/b.c d_e"), {"-a-b-c-d-e"} | {self.slug(os.path.realpath("/a/b.c d_e"))})
        self.assertIn("C--Users-u-x", inbox.project_slugs(r"C:\Users\u\x"))

    def test_exact_folder_only(self):
        own = self.make(self.slug(self.work))
        longer = self.make(self.slug(self.work) + "-2")
        other = self.make(self.slug(self.tmp / "elsewhere" / "work"))
        inbox.remove_project_dirs(self.work)
        self.assertFalse(own.exists())
        self.assertTrue(longer.exists())
        self.assertTrue(other.exists())

    def test_sweep_takes_only_session_folders(self):
        session = self.make(self.slug(self.runs / "comment-2" / "work"))
        not_a_session = self.make(self.slug(self.runs / "comment-2" / "notes"))
        outside = self.make(self.slug(self.tmp / "work"))
        inbox.remove_project_dirs(self.runs, prefix=True)
        self.assertFalse(session.exists())
        self.assertTrue(not_a_session.exists())
        self.assertTrue(outside.exists())

    def test_links_and_files_are_left_alone(self):
        target = self.tmp / "precious"
        target.mkdir()
        (target / "keep").write_text("x", encoding="utf-8")
        name = self.slug(self.work)
        try:
            (self.projects / name).symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this system cannot make a symbolic link here")
        inbox.remove_project_dirs(self.work)
        self.assertTrue((target / "keep").exists())

    def test_cut_long_names(self):
        # Just past 200 characters, so the path stays under Windows' 260 where it has no long paths.
        part = max(1, (215 - len(str(self.runs))) // 2)
        deep = self.runs / ("k" * part) / ("w" * part) / "work"
        slug = self.slug(deep)
        self.assertGreater(len(slug), 200)
        try:
            deep.mkdir(parents=True)
            cut = self.make(slug[:200] + "-1a2b3c")
            uncut = self.make(slug[:199] + "x")
        except OSError:
            self.skipTest("this system cannot hold paths that long here")
        inbox.remove_project_dirs(deep)
        self.assertFalse(cut.exists())
        self.assertTrue(uncut.exists())


if __name__ == "__main__":
    unittest.main()
