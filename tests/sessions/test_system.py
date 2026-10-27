"""What differs per system (unio_sessions/system.py): locks, a process's name,
parent and start time, whether a Claude Code is a child session, and the
detached start. Each test runs the real calls of the system it runs on;
the other systems' parsing runs everywhere on recorded shapes."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import SCRIPTS, WINDOWS, remove_tree, wait_for
from unio_sessions import hook as session_hook
from unio_sessions import system

LINUX = sys.platform.startswith("linux")
MACOS = sys.platform == "darwin"
CHILD = [sys.executable, "-c", "import sys, time; print('up', flush=True); time.sleep(60)"]


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-system-test-"))
        self.addCleanup(remove_tree, self.tmp)

    def spawn(self, argv=CHILD, **kw):
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, **kw)
        self.addCleanup(self.end, proc)
        self.assertEqual(proc.stdout.readline().strip(), "up")
        return proc

    @staticmethod
    def end(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


class Locks(Case):
    def test_a_lock_another_process_holds_is_refused_until_it_lets_go(self):
        lock = self.tmp / "x.watch.lock"
        holder = self.spawn([sys.executable, "-c", f"""
import sys, time
sys.path.insert(0, {str(SCRIPTS)!r})
from unio_sessions import system
f = open({str(lock)!r}, "a")
assert system.try_lock(f)
print("up", flush=True)
time.sleep(60)
"""])
        with open(lock, "a") as f:
            self.assertFalse(system.try_lock(f), "two processes held one lock")
        self.end(holder)
        with open(lock, "a") as f:
            self.assertTrue(wait_for(self, lambda: system.try_lock(f), 5, "the lock outlived its holder"))
            system.unlock(f)

    def test_the_hooks_lock_waits_then_gives_up(self):
        with mock.patch.object(session_hook, "STATE", self.tmp):
            with session_hook.Locked("s", wait=0) as first:
                self.assertTrue(first)
                started = time.monotonic()
                with session_hook.Locked("s", wait=0.3) as second:
                    self.assertFalse(second)
                self.assertGreaterEqual(time.monotonic() - started, 0.3)
            with session_hook.Locked("s", wait=0) as again:
                self.assertTrue(again, "a released lock stayed taken")

    def test_a_replace_is_retried_while_windows_holds_the_file(self):
        # Windows refuses to replace a file another process has open; the hook retries.
        target = self.tmp / "p.json"
        target.write_text("{}")
        calls = []
        real = os.replace

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("in use")
            return real(src, dst)

        tmp = self.tmp / "p.json.tmp"
        tmp.write_text('{"a": 1}')
        with mock.patch.object(system, "WINDOWS", True), mock.patch("os.replace", side_effect=flaky):
            system.replace(tmp, target)
        self.assertEqual((len(calls), json.loads(target.read_text())), (3, {"a": 1}))


class Processes(Case):
    def test_a_live_process_has_a_stable_start_and_none_once_gone(self):
        proc = self.spawn()
        first = system.start_time(proc.pid)
        self.assertTrue(first)
        time.sleep(0.2)
        self.assertEqual(system.start_time(proc.pid), first)
        self.end(proc)
        self.assertIsNone(system.start_time(proc.pid))

    def test_this_process_is_named_by_its_executable_with_its_parent(self):
        name, parent, started = system.process(os.getpid())
        self.assertIn("python", name)
        self.assertEqual(parent, os.getppid())
        self.assertEqual(started, system.start_time(os.getpid()))

    def test_the_walk_finds_the_claude_pid_claude_code_gives_its_hooks(self):
        # A process named python, not claude: only CLAUDE_PID ties it to the hook.
        probe = ("import sys; sys.path.insert(0, %r); from unio_sessions import system; "
                 "print(system.claude_process())" % str(SCRIPTS))
        parent = self.spawn([sys.executable, "-c", f"""
import json, os, subprocess, sys
print("up", flush=True)
env = dict(os.environ, CLAUDE_PID=str(os.getpid()))
with_pid = subprocess.run([sys.executable, "-c", {probe!r}], env=env, capture_output=True, text=True).stdout
env.pop("CLAUDE_PID")
without = subprocess.run([sys.executable, "-c", {probe!r}], env=env, capture_output=True, text=True).stdout
print(json.dumps([with_pid.strip(), without.strip()]), flush=True)
"""])
        with_pid, without = json.loads(parent.stdout.readline())
        self.assertEqual(with_pid, str((parent.pid, system.start_time(parent.pid))))
        self.assertEqual(without, "None", "a process that is neither Claude Code nor a shell was taken for it")

    @unittest.skipIf(WINDOWS, "a POSIX shell between them")
    def test_the_walk_passes_the_shells_between_claude_code_and_the_hook(self):
        probe = ("import sys; sys.path.insert(0, %r); from unio_sessions import system; "
                 "print(system.claude_process())" % str(SCRIPTS))
        parent = self.spawn([sys.executable, "-c", f"""
import os, shlex, subprocess, sys
print("up", flush=True)
env = dict(os.environ, CLAUDE_PID=str(os.getpid()))
inner = "sh -c " + shlex.quote(shlex.quote(sys.executable) + " -c " + shlex.quote({probe!r}) + "; true")
print(subprocess.run(["sh", "-c", inner + "; true"], env=env, capture_output=True, text=True).stdout.strip(),
      flush=True)
"""])
        self.assertEqual(parent.stdout.readline().strip(), str((parent.pid, system.start_time(parent.pid))))

    @unittest.skipIf(WINDOWS, "Windows reads no other process's environment")
    def test_a_claude_code_started_as_a_child_session_says_so_in_its_own_environment(self):
        child = self.spawn(env=dict(os.environ, CLAUDE_CODE_CHILD_SESSION="1"))
        plain_env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_CHILD_SESSION"}
        plain = self.spawn(env=plain_env)
        self.assertTrue(system.is_child(child.pid))
        self.assertFalse(system.is_child(plain.pid))

    def test_a_process_with_no_claude_code_above_it_is_no_child(self):
        # On Windows this is the real ancestor walk: no Claude Code runs above the tests.
        plain = self.spawn(env={k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_CHILD_SESSION"})
        self.assertFalse(system.is_child(plain.pid))

    def test_a_gone_process_is_no_child(self):
        proc = self.spawn()
        self.end(proc)
        self.assertFalse(system.is_child(proc.pid))


class OtherSystems(unittest.TestCase):
    """The parsing of the systems these tests are not running on."""

    def test_ps_names_a_process_by_its_executable_and_lstart(self):
        out = "  321 Ss   Mon Oct  5 09:04:07 2026     /Users/someone/.local/share/claude/versions/claude\n"
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout=out)):
            self.assertEqual(system._ps_process(4), ("claude", 321, "Mon Oct 5 09:04:07 2026"))
        for out in ("", "  321 Z    Mon Oct  5 09:04:07 2026 sh\n", "garbage\n"):
            with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout=out)):
                self.assertIsNone(system._ps_process(4), out)

    def test_a_login_shell_and_an_exe_are_named_plainly(self):
        self.assertEqual(system.base_name("-zsh"), "zsh")
        self.assertEqual(system.base_name("C:\\Users\\x\\.local\\bin\\Claude.exe"), "claude")
        self.assertEqual(system.base_name("/bin/sh"), "sh")

    def test_procargs2_holds_the_environment_after_the_arguments(self):
        raw = ((2).to_bytes(4, sys.byteorder) + b"/usr/local/bin/claude\0\0\0\0claude\0--resume\0"
               b"HOME=/Users/x\0CLAUDE_CODE_CHILD_SESSION=1\0\0\0junk")
        self.assertEqual(system.parse_procargs(raw), [b"HOME=/Users/x", b"CLAUDE_CODE_CHILD_SESSION=1"])
        self.assertEqual(system.parse_procargs(b"\0\0"), [])

    def test_windows_finds_a_claude_code_above_a_child_and_none_above_a_top_level_one(self):
        # pid: (name, parent); start times grow with the pid, as creation times do.
        table = {10: ("explorer", 1), 20: ("claude", 10), 30: ("bash", 20), 40: ("claude", 30), 50: ("pwsh", 10),
                 60: ("claude", 50)}
        start = {pid: str(pid * 1000) for pid in table}
        with mock.patch.object(system, "WINDOWS", True), \
                mock.patch.object(system, "_windows_table", return_value=table), \
                mock.patch.object(system, "_windows_start", side_effect=lambda pid: start.get(pid)):
            self.assertTrue(system.is_child(40), "a claude started from another session's shell")
            self.assertFalse(system.is_child(60))
            self.assertFalse(system.is_child(20))
            # A parent id that Windows reused for a newer process is no parent.
            start[30] = str(10 ** 9)
            self.assertFalse(system.is_child(40))

    def test_windows_keeps_the_sessions_in_local_app_data_unless_a_drive_is_named(self):
        with mock.patch.object(system, "WINDOWS", True):
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Users\\x\\AppData\\Local", "XDG_STATE_HOME": ""}):
                self.assertEqual(system.state_dir(), Path("C:\\Users\\x\\AppData\\Local") / "unio" / "sessions")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:\\L", "XDG_STATE_HOME": "/c/Users/x/.state"}):
                self.assertEqual(system.state_dir(), Path("C:\\L") / "unio" / "sessions", "an MSYS path was taken")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:\\L", "XDG_STATE_HOME": "D:/state"}):
                self.assertEqual(system.state_dir(), Path("D:/state") / "unio" / "sessions")

    def test_linux_and_macos_keep_the_hand_made_hooks_folder(self):
        with mock.patch.object(system, "WINDOWS", False):
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": ""}):
                self.assertEqual(system.state_dir(), Path.home() / ".local" / "state" / "unio" / "sessions")
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/x/state"}):
                self.assertEqual(system.state_dir(), Path("/x/state") / "unio" / "sessions")

    def test_windows_detaches_from_the_console_group_and_job(self):
        calls = []

        def popen(args, **kw):
            calls.append(kw.get("creationflags"))
            if len(calls) == 1:
                raise PermissionError("the job forbids breaking away")
            return "started"

        with mock.patch.object(system, "WINDOWS", True), mock.patch.object(subprocess, "Popen", side_effect=popen):
            self.assertEqual(system.detach(["-c", "pass"], background=True), "started")
        self.assertEqual(calls, [0x01000000 | 0x08000000 | 0x4000 | 0x200 | 0x8, 0x08000000 | 0x4000 | 0x200 | 0x8])


class Detach(Case):
    def test_a_detached_process_runs_on_its_own(self):
        out = self.tmp / "out.json"
        code = ("import json, os, sys; "
                "json.dump({'pid': os.getpid(), 'sid': os.getsid(0) if hasattr(os, 'getsid') else None, "
                "'exe': sys.executable, 'cwd': os.getcwd()}, open(%r, 'w'))" % str(out))
        proc = system.detach(["-c", code], background=True)
        wait_for(self, lambda: out.exists() and out.stat().st_size, 10, "the detached process never ran")
        seen = json.loads(wait_for(self, lambda: self.read(out), 5, "its report never landed"))
        self.assertEqual(seen["pid"], proc.pid)
        self.assertEqual(Path(seen["cwd"]), Path(os.path.abspath(os.sep)))
        if WINDOWS:
            windowless = Path(sys.executable).with_name("pythonw.exe")
            if windowless.is_file():
                self.assertEqual(Path(seen["exe"]).name.lower(), "pythonw.exe")
        else:
            self.assertEqual(seen["sid"], proc.pid, "it shares the hook's session, so a hangup would end it")
        proc.wait(timeout=10)

    @staticmethod
    def read(path):
        try:
            text = path.read_text()
            json.loads(text)
            return text
        except (OSError, ValueError):
            return None


if __name__ == "__main__":
    unittest.main()
