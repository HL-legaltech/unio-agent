"""bin/unio and bin/unio.cmd, the one command people type: its help table,
its dispatch to unio-runner, and the command unio-runner's hints name."""

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
        self.assertEqual(commands_in(help_text()), ["runner"])
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
        """bin/unio through sh, or `command` as given; returns (code, stdout, stderr)."""
        done = subprocess.run(command or [SH, str(BIN / "unio")] + list(args), stdin=subprocess.DEVNULL,
                              capture_output=True, encoding="utf-8", errors="replace", env=self.box.env(**env),
                              cwd=str(self.box.root), timeout=180)
        return done.returncode, done.stdout, done.stderr

    def test_help(self):
        for args in (["help"], ["-h"], ["--help"], [], [""]):
            code, out, err = self.unio(*args)
            self.assertEqual((code, out, err), (0, help_text(), ""), args)
        self.assertTrue(help_text().startswith("Uso: unio <comando> [opções]\n"))

    def test_version_is_the_runners(self):
        self.assertEqual(self.unio("--version"), (0, VERSION + "\n", ""))

    def test_anything_else_is_a_usage_error(self):
        for word in ("bogus", "Runner", "--runner", "setup"):
            code, out, err = self.unio(word, "status")
            self.assertEqual((code, out), (2, ""), word)
            self.assertEqual(err, "Comando desconhecido: %s. Rode unio help.\n" % word)

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
            self.assertEqual(commands_in(out), ["runner"])

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
