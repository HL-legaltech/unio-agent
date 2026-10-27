"""unio resume, by task and by session id, in a scratch HOME, run as bin/unio
and bin/unio.cmd run it."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from support import ENTRY, REPO, WINDOWS, base_env, posix_path, remove_tree


class Resume(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-resume-test-"))
        self.addCleanup(remove_tree, self.tmp)
        self.projects = self.tmp / ".claude" / "projects"
        self.state = self.tmp / "state" / "unio" / "sessions"
        self.state.mkdir(parents=True)
        self.log = self.tmp / "claude.log"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        # Stands in for `claude --resume`: says where it ran and with what.
        if WINDOWS:
            (self.bin / "claude.cmd").write_text(f'@echo %CD% %*>> "{self.log}"\r\n')
        else:
            (self.bin / "claude").write_text(f'#!/bin/sh\necho "$PWD $*" >> "{self.log}"\n')
            (self.bin / "claude").chmod(0o755)
        self.env = base_env(self.tmp)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]

    def session(self, cwd, folder="-project", session_id=None):
        session_id = session_id or str(uuid.uuid4())
        Path(cwd).mkdir(parents=True, exist_ok=True)
        path = self.projects / folder / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "last-prompt"}) + "\n"
                        + json.dumps({"type": "user", "cwd": str(cwd), "sessionId": session_id}) + "\n")
        return session_id

    def run_resume(self, *args):
        return subprocess.run([sys.executable, "-I", str(ENTRY), "resume", *args], capture_output=True, text=True,
                              env=self.env, stdin=subprocess.DEVNULL, timeout=30, encoding="utf-8")

    def ran(self):
        return self.log.read_text().strip() if self.log.exists() else ""

    def test_a_session_id_resumes_that_session_in_its_folder(self):
        cwd = self.tmp / "work" / "unio"
        sid = self.session(cwd)
        p = self.run_resume(sid)
        self.assertEqual(p.returncode, 0, p.stderr)
        if WINDOWS:
            self.assertIn(f"claude --resume {sid}, na pasta {cwd}", p.stdout)
        else:
            self.assertIn(f"Retomando: cd ~/work/unio && claude --resume {sid}", p.stdout)
        if WINDOWS:
            # cmd may name the folder by its short 8.3 form.
            self.assertTrue(self.ran().endswith(f"unio --resume {sid}"), self.ran())
        else:
            # macOS's temporary folder is under /var, a link to /private/var.
            ran_in, _, rest = self.ran().partition(" --resume ")
            self.assertEqual((os.path.realpath(ran_in), rest), (os.path.realpath(cwd), sid))

    @unittest.skipIf(WINDOWS, "Windows names the folder in words, not as a cd")
    def test_the_printed_command_quotes_a_folder_with_spaces(self):
        sid = self.session(self.tmp / "work" / "Proj Alpha")
        p = self.run_resume(sid, "--print")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"cd ~/'work/Proj Alpha' && claude --resume {sid}", p.stdout)

    def test_its_first_8_characters_do_while_no_other_session_starts_with_them(self):
        sid = self.session(self.tmp / "a")
        p = self.run_resume(sid[:8], "--print")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"claude --resume {sid}", p.stdout)
        self.assertFalse(self.log.exists(), "--print ran it")
        other = sid[:8] + str(uuid.uuid4())[8:]
        self.session(self.tmp / "b", folder="-other", session_id=other)
        p = self.run_resume(sid[:8], "--print")
        self.assertEqual(p.returncode, 1)
        self.assertIn("digite mais caracteres", p.stderr)
        self.assertIn(sid, p.stderr)
        self.assertIn(other, p.stderr)

    def test_an_unknown_session_id_says_so(self):
        p = self.run_resume(str(uuid.uuid4()))
        self.assertEqual(p.returncode, 1)
        self.assertIn("nenhuma sessão ligada a uma tarefa tem esse id", p.stderr)

    def test_a_session_whose_folder_is_gone_says_so(self):
        cwd = self.tmp / "work" / "worktree"
        sid = self.session(cwd)
        cwd.rmdir()
        for flags in ((), ("--print",)):
            with self.subTest(flags):
                p = self.run_resume(sid, *flags)
                self.assertEqual(p.returncode, 1)
                self.assertNotIn("Traceback", p.stderr)
                self.assertIn("que não existe mais", p.stderr)
                self.assertIn(f"claude --resume {sid}", p.stderr)
        self.assertFalse(self.log.exists())

    def test_a_session_whose_starting_folder_is_unknown_is_never_resumed_elsewhere(self):
        sid = str(uuid.uuid4())
        path = self.projects / "-project" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "last-prompt"}) + "\n")
        p = self.run_resume(sid)
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("não diz em que pasta ela começou", p.stderr)
        self.assertFalse(self.log.exists(), "claude ran in a folder the session never started in")

    def test_a_task_finds_the_sessions_bound_to_it(self):
        sid = self.session(self.tmp / "work" / "backend")
        task = {"id": str(uuid.uuid4()), "key": "DEV-439", "url": "https://unio.so/app/tasks/DEV-439"}
        (self.state / f"{sid}.json").write_text(json.dumps({"task": task}))
        for ref in ("DEV-439", "dev-439", task["id"], "https://unio.so/app/tasks/DEV-439/slug?ws=1"):
            with self.subTest(ref):
                p = self.run_resume(ref, "--print")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn(f"claude --resume {sid}", p.stdout)

    def test_the_sessions_that_worked_on_it_come_before_those_that_filed_it(self):
        worked = self.session(self.tmp / "w")
        filed = self.session(self.tmp / "f")
        task = {"id": str(uuid.uuid4()), "key": "DEV-9"}
        (self.state / f"{worked}.json").write_text(json.dumps({"task": task}))
        (self.state / f"{filed}.json").write_text(json.dumps({"task": {"id": str(uuid.uuid4()), "key": "DEV-8"},
                                                              "follow_ups": [task]}))
        p = self.run_resume("DEV-9", "--list")
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], "DEV-9: 2 sessões")
        self.assertIn(worked, lines[1])
        self.assertIn(filed, lines[2])
        self.assertIn("criou esta tarefa a partir de DEV-8", lines[2])
        self.assertFalse(self.log.exists(), "--list ran it")

    def test_a_task_no_session_knows_says_so_in_portuguese(self):
        p = self.run_resume("DEV-1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("Nenhuma sessão deste computador está ligada a DEV-1", p.stderr)

    def test_a_bad_call_points_to_the_help(self):
        for args in ((), ("DEV-1", "DEV-2"), ("DEV-1", "--bogus")):
            with self.subTest(args):
                p = self.run_resume(*args)
                self.assertEqual(p.returncode, 2)
                self.assertIn("Rode unio help resume.", p.stderr)


@unittest.skipIf(WINDOWS, "bin/unio is the POSIX command; CI runs bin/unio.cmd resume on Windows")
class Command(unittest.TestCase):
    """bin/unio resume finds Python itself, and says so when there is none."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-resume-cmd-"))
        self.addCleanup(remove_tree, self.tmp)

    def unio(self, *args, path=None):
        env = base_env(self.tmp)
        if path is not None:
            env["PATH"] = path
        return subprocess.run([shutil.which("sh"), str(REPO / "bin" / "unio"), *args], capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL, timeout=30)

    def test_it_runs_the_resume_logic(self):
        p = self.unio("resume", "DEV-1")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("Nenhuma sessão deste computador está ligada a DEV-1", p.stderr)

    def test_help_and_bad_options_never_need_python(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        for name in ("sed", "tr", "cut", "readlink", "dirname", "cat"):
            for folder in posix_path().split(os.pathsep):
                if (Path(folder) / name).exists():
                    (empty / name).symlink_to(Path(folder) / name)
                    break
        p = self.unio("resume", "--help", path=str(empty))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("Uso: unio resume"), p.stdout)
        p = self.unio("resume", "--bogus", path=str(empty))
        self.assertEqual(p.returncode, 2)
        p = self.unio("resume", "DEV-1", path=str(empty))
        self.assertEqual(p.returncode, 1)
        self.assertIn("unio resume precisa do Python 3.9 ou mais novo", p.stderr)

    def test_help_lists_resume(self):
        p = self.unio("help")
        self.assertIn("  resume      Retoma a sessão", p.stdout)


if __name__ == "__main__":
    unittest.main()
