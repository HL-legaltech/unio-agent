"""bin/unio and bin/unio.cmd, the one command people type: its help table,
its dispatch to unio-runner, and the command unio-runner's hints name."""

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from runner_support import RUNNER_DIR, Sandbox, load_runner, write_program

ur = load_runner()
BIN = RUNNER_DIR.parent / "bin"
VERSION = (RUNNER_DIR / "VERSION").read_text(encoding="utf-8").strip()
SH = shutil.which("sh")
COMMANDS = ["connect", "status", "doctor", "update", "disconnect", "resume", "runner"]
# The commands whose help and options bin/unio itself handles.
OWN = COMMANDS[:-1]


def help_text():
    """`unio help` as bin/unio's "#:" lines say it."""
    lines = (BIN / "unio").read_text(encoding="utf-8").splitlines()
    return "".join(line[3:] + "\n" for line in lines if line.startswith("#:"))


def commands_in(text):
    """The command names a help lists, under Comandos:."""
    lines = text.splitlines()
    names = []
    for line in lines[lines.index("Comandos:") + 1:]:
        if not line.strip():
            break
        names.append(line.split()[0])
    return names


class Table(unittest.TestCase):
    """The help's table lists exactly the commands each file runs."""

    def test_the_table_lists_what_bin_unio_runs(self):
        arms = re.findall(r"^  ([^ ()]+)\)$", (BIN / "unio").read_text(encoding="utf-8"), re.M)
        words = [w for arm in arms for w in arm.split("|") if not w.startswith("-") and w not in ("help", "*")]
        self.assertEqual(commands_in(help_text()), COMMANDS)
        self.assertEqual(words, commands_in(help_text()))

    def test_the_table_lists_what_unio_cmd_runs(self):
        text = (BIN / "unio.cmd").read_bytes().decode("ascii")
        words = re.findall(r'^if /i "%unio_cmd%"=="([^"]+)" goto', text, re.M)
        self.assertEqual([w for w in words if not w.startswith("-") and w != "help"], commands_in(help_text()))

    def test_unio_cmd_is_ascii_with_crlf(self):
        # cmd reads it in the console's code page, and can miss a label in a file with bare LFs.
        data = (BIN / "unio.cmd").read_bytes()
        self.assertTrue(all(b < 128 for b in data))
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))


@unittest.skipIf(os.name == "nt" or not SH, "bin/unio is the macOS and Linux command")
class PosixCommand(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)

    def unio(self, *args, command=None, **env):
        """bin/unio through sh, or `command` as given; returns (code, stdout, stderr).

        In a session of its own, so a test run from a terminal leaves bin/unio
        no /dev/tty to ask on, as in CI.
        """
        done = subprocess.run(command or [SH, str(BIN / "unio")] + list(args), stdin=subprocess.DEVNULL,
                              capture_output=True, encoding="utf-8", errors="replace", env=self.box.env(**env),
                              cwd=str(self.box.root), timeout=180, start_new_session=True)
        return done.returncode, done.stdout, done.stderr

    def test_help(self):
        for args in (["help"], ["-h"], ["--help"], [], [""]):
            code, out, err = self.unio(*args)
            self.assertEqual((code, out, err), (0, help_text(), ""), args)
        self.assertTrue(help_text().startswith("Uso: unio <comando> [opções]\n"))

    def test_help_runner_is_the_runners_own(self):
        runner_help = self.unio("runner", "--help")
        self.assertEqual(runner_help[0], 0)
        self.assertTrue(runner_help[1].startswith("Uso: unio runner <comando> [opções]"))
        for args in (["help", "runner"], ["-h", "runner"], ["--help", "runner"], ["runner"]):
            self.assertEqual(self.unio(*args), runner_help, args)

    def test_version_is_the_runners(self):
        self.assertEqual(self.unio("--version"), (0, VERSION + "\n", ""))

    def test_anything_else_is_a_usage_error(self):
        for word in ("bogus", "Runner", "--runner", "setup"):
            code, out, err = self.unio(word, "status")
            self.assertEqual((code, out), (2, ""), word)
            self.assertEqual(err, "Comando desconhecido: %s. Rode unio help.\n" % word)

    def test_each_command_has_its_own_help(self):
        for name in OWN:
            own = self.unio(name, "--help")
            self.assertEqual(own[0], 0, name)
            self.assertTrue(own[1].startswith("Uso: unio %s" % name), own[1])
            self.assertEqual(own[2], "")
            self.assertEqual(self.unio("help", name), own, name)

    def test_a_command_refuses_an_unknown_option(self):
        for name in OWN:
            code, out, err = self.unio(name, "--bogus")
            self.assertEqual((code, out), (2, ""), name)
            self.assertEqual(err, "Opção desconhecida: --bogus.\nRode unio help %s.\n" % name)

    def write(self, relative, text):
        path = self.box.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_status_with_nothing_connected(self):
        code, out, err = self.unio("status")
        self.assertEqual((code, err), (1, ""))
        self.assertIn("Nenhum workspace está conectado neste computador. Rode unio connect.", out)

    def test_disconnect_needs_a_workspace(self):
        code, out, err = self.unio("disconnect", "--yes")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Nenhum workspace vale nesta pasta", err)
        code, _, err = self.unio("disconnect", "--workspace", "org_Missing", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("O workspace org_Missing não está conectado neste computador", err)

    def test_disconnect_removes_only_that_workspace(self):
        a = self.write(".config/unio/workspaces/org_A/token", "unio_" + "a" * 46)
        b = self.write(".config/unio/workspaces/org_B/token", "unio_" + "b" * 46)
        self.write(".config/unio/default-workspace", "org_A\n")
        projects = self.write(".config/unio/projects", "%s\torg_B\n/elsewhere\torg_A\n" % self.box.root)
        # Without a terminal to ask, it refuses before touching anything.
        code, _, err = self.unio("disconnect", "--workspace", "org_B")
        self.assertEqual(code, 2)
        self.assertIn("rode de novo com --yes", err)
        self.assertTrue(b.exists())
        code, out, err = self.unio("disconnect", "--yes")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Token do workspace org_B apagado deste computador.", out)
        self.assertIn("revogue-o em Configurações > Pessoal > Agente de código", out)
        self.assertFalse(b.exists())
        self.assertTrue(a.exists())
        self.assertEqual(projects.read_text(encoding="utf-8"), "/elsewhere\torg_A\n")
        self.assertEqual((self.box.home / ".config/unio/default-workspace").read_text(encoding="utf-8"), "org_A\n")

    def test_doctor_json_is_a_list_and_exits_1_on_a_problem(self):
        code, out, err = self.unio("doctor", "--json")
        self.assertEqual((code, err), (1, ""))
        items = json.loads(out)
        self.assertTrue(items)
        self.assertTrue(all(set(item) == {"status", "area", "check", "detail"} for item in items))
        self.assertIn(("bad", "token"), [(item["status"], item["check"]) for item in items])
        # A terminal names the unio command where Claude Code is told /unio:connect.
        code, out, _ = self.unio("doctor")
        self.assertEqual(code, 1)
        self.assertIn("rode unio connect", out)
        self.assertNotIn("/unio:connect", out)

    def test_runner_runs_unio_runner_with_the_rest(self):
        code, out, _ = self.unio("runner", "--help")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Uso: unio runner <comando> [opções]"))
        code, out, _ = self.unio("runner", "status")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Nenhum serviço de respostas está configurado neste computador."), out)
        # unio-runner's own exit code comes back.
        code, out, _ = self.unio("runner", "bogus")
        self.assertEqual((code, out), (2, "Comando desconhecido: bogus. Rode unio runner help.\n"))

    def test_runner_without_python(self):
        code, out, err = self.unio("runner", "status", PATH=str(self.box.root / "nothing"))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("O comando python3 não está no PATH.", err)

    def test_runner_with_a_python3_that_does_not_run(self):
        # As macOS's /usr/bin/python3 without the Command Line Tools, a stub that offers to install them.
        stub = self.box.root / "stub"
        stub.mkdir()
        (stub / "python3").write_text('#!/bin/sh\nprintf "%%s\\n" "$1" >> "%s"\n'
                                      'echo "xcode-select: note: No developer tools were found." >&2\nexit 1\n'
                                      % (stub / "calls"), encoding="utf-8")
        (stub / "python3").chmod(0o755)
        code, out, err = self.unio("runner", "status", PATH=str(stub))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "O serviço de respostas precisa do Python 3.9 ou mais novo. Instale um Python mais "
                              "novo e rode este comando de novo.\n")
        # It was only asked for its version, never handed the runner.
        self.assertEqual((stub / "calls").read_text(encoding="utf-8"), "-c\n")

    def test_through_links_on_path(self):
        # As install.sh links it into ~/.local/bin, and a second, relative link on the way.
        first, second = self.box.root / "first", self.box.root / "second"
        first.mkdir()
        second.mkdir()
        (first / "unio").symlink_to(BIN / "unio")
        (second / "unio").symlink_to(Path("..") / "first" / "unio")
        path = str(second) + os.pathsep + self.box.env()["PATH"]
        self.assertEqual(self.unio(command=["unio", "--version"], PATH=path), (0, VERSION + "\n", ""))
        self.assertEqual(self.unio(command=["unio", "help"], PATH=path)[1], help_text())
        # unio-runner then names the command as people type it.
        code, out, _ = self.unio(command=["unio", "runner", "start", "--workspace", "org_test"], PATH=path)
        self.assertEqual(code, 1, out)
        self.assertIn("Rode primeiro: unio runner setup --workspace org_test", out)


@unittest.skipUnless(os.name == "nt", "bin/unio.cmd is the Windows command")
class WindowsCommand(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)

    def unio(self, *args, **env):
        done = subprocess.run([str(BIN / "unio.cmd")] + list(args), stdin=subprocess.DEVNULL, capture_output=True,
                              env=self.box.env(**env), cwd=str(self.box.root), timeout=180)
        return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")

    def test_help(self):
        for args in (["help"], ["-h"], ["--help"], []):
            code, out, _ = self.unio(*args)
            self.assertEqual(code, 0, args)
            self.assertTrue(out.startswith("Uso: unio <comando>"), out)
            self.assertEqual(commands_in(out), COMMANDS)

    def test_help_runner_is_the_runners_own(self):
        for args in (["help", "runner"], ["runner"]):
            code, out, _ = self.unio(*args)
            self.assertEqual(code, 0, args)
            self.assertTrue(out.startswith("Uso: unio runner <comando> [opções]"), out)

    def test_version_is_the_runners(self):
        code, out, _ = self.unio("--version")
        self.assertEqual((code, out.strip()), (0, VERSION))

    def test_anything_else_is_a_usage_error(self):
        code, out, err = self.unio("bogus", "status")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Comando desconhecido: bogus. Rode unio help.", err)

    def test_runner_runs_unio_runner_with_the_rest(self):
        code, out, _ = self.unio("runner", "--help")
        self.assertEqual(code, 0, out)
        self.assertIn("Uso: unio runner <comando>", out)
        code, out, _ = self.unio("runner", "status")
        self.assertEqual(code, 0, out)
        self.assertIn("Nenhum serviço de respostas está configurado neste computador.", out)
        code, out, _ = self.unio("runner", "bogus")
        self.assertEqual(code, 2, out)
        # With bin on PATH, unio-runner names the command as people type it.
        code, out, _ = self.unio("runner", "start", "--workspace", "org_test",
                                 PATH=str(BIN) + os.pathsep + self.box.env()["PATH"])
        self.assertEqual(code, 1, out)
        self.assertIn("Rode primeiro: unio runner setup --workspace org_test", out)

    def test_the_other_commands_run_through_gits_sh(self):
        # bin/unio itself, by Git for Windows' sh.exe: the same words as on macOS and Linux.
        code, out, _ = self.unio("help", "connect")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("Uso: unio connect"), out)
        code, out, _ = self.unio("status")
        self.assertEqual(code, 1, out)
        self.assertIn("Nenhum workspace está conectado neste computador. Rode unio connect.", out)
        code, out, err = self.unio("connect", "--bogus")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Opção desconhecida: --bogus.", err)

    def test_resume_runs_python_in_this_console_and_its_help_through_gits_sh(self):
        code, out, err = self.unio("resume", "DEV-1")
        self.assertEqual(code, 1, out + err)
        self.assertIn("Nenhuma sessão deste computador está ligada a DEV-1", err)
        for args in (["resume", "--help"], ["help", "resume"]):
            code, out, _ = self.unio(*args)
            self.assertEqual(code, 0, args)
            self.assertTrue(out.startswith("Uso: unio resume"), out)
        code, _, err = self.unio("resume", "DEV-1", "--bogus")
        self.assertEqual(code, 2)
        self.assertIn("Opção desconhecida: --bogus.", err)

    def test_resume_without_a_python_that_runs(self):
        stub = self.box.root / "stub"
        stub.mkdir()
        (stub / "python.cmd").write_bytes(b'@echo off\r\nexit /b 9009\r\n')
        system = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32")
        code, out, err = self.unio("resume", "DEV-1", PATH=str(stub) + os.pathsep + system)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Falta o Python 3.9 ou mais novo", err)

    def test_without_git_for_windows(self):
        system = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32")
        code, out, err = self.unio("status", PATH=system)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Falta o Git for Windows", err)

    def test_runner_without_a_python_that_runs(self):
        # The Microsoft Store's python.exe alias opens the Store instead of running Python.
        stub = self.box.root / "stub"
        stub.mkdir()
        (stub / "python.cmd").write_bytes(b'@echo off\r\n>>"%~dp0calls" echo(%~1\r\nexit /b 9009\r\n')
        system = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32")
        code, out, err = self.unio("runner", "status", PATH=str(stub) + os.pathsep + system)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Falta o Python 3.9 ou mais novo", err)
        self.assertEqual((stub / "calls").read_text(encoding="utf-8").split(), ["-c"])


class Hints(unittest.TestCase):
    """The command unio-runner's hints name, in this process."""

    def setUp(self):
        self.box = Sandbox(self)

    def test_unio_runner_when_unio_on_path_is_this_clone(self):
        with self.box.inside(PATH=str(BIN) + os.pathsep + str(self.box.bin)):
            self.assertEqual(ur.self_command(), "unio runner")

    def test_the_clones_unio_by_its_path_otherwise(self):
        shim = BIN / ("unio.cmd" if os.name == "nt" else "unio")
        with self.box.inside(PATH=str(self.box.bin)):
            expected = '& "%s" runner' % shim if os.name == "nt" else ur.shown(shim) + " runner"
            self.assertEqual(ur.self_command(), expected)
        # Another program named unio comes first on PATH.
        write_program(self.box.bin, "unio", str(self.box.lib / "claude.py"))
        with self.box.inside(PATH=str(self.box.bin) + os.pathsep + str(BIN)):
            self.assertEqual(ur.self_command(), expected)


if __name__ == "__main__":
    unittest.main()
