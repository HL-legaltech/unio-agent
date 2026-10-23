"""unio-runner's arguments and its commands after setup: status, logs,
start/stop/restart, uninstall and update."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner_support import RUNNER, Sandbox, captured, load_runner

ur = load_runner()


class Arguments(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ur.parse_args(["setup", "--workspace", "org_x", "--yes", "--api=https://a.example/v1"]),
                         ("setup", {"--workspace": "org_x", "--yes": True, "--api": "https://a.example/v1"}))
        self.assertEqual(ur.parse_args(["update"]), ("update", {}))
        self.assertEqual(ur.parse_args(["status"]), ("status", {}))
        for argv in ([], ["-h"], ["--help"], ["help"], ["logs", "--help"]):
            self.assertEqual(ur.parse_args(argv)[0], "help", argv)

    def test_refusals_are_portuguese_and_exit_2(self):
        for argv, text in ((["bogus"], "Comando desconhecido: bogus."),
                           (["setup", "--workspace", "org_x", "--token", RUNNER], "Opção desconhecida para setup: --token."),
                           (["logs", "--workspace"], "Falta o valor de --workspace."),
                           (["start"], "Falta --workspace org_..., o workspace do serviço."),
                           (["setup", "--workspace", "org_x", "--yes=no"], "--yes não leva valor."),
                           (["update", "--workspace", "org_x"], "Opção desconhecida para update: --workspace.")):
            with self.assertRaises(ur.Stop) as caught:
                ur.parse_args(argv)
            self.assertEqual(caught.exception.code, 2)
            self.assertIn(text, caught.exception.message)
            self.assertNotIn(RUNNER, caught.exception.message)

    def test_help_is_portuguese(self):
        with captured() as out:
            self.assertEqual(ur.main(["--help"]), 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("Uso: unio-runner <comando> [opções]"))
        for command in ("setup", "status", "start", "stop", "restart", "logs", "uninstall", "update"):
            self.assertIn("  " + command, text)

    def test_api_addresses(self):
        self.assertEqual(ur.normalize_api("https://api.unio.so"), "https://api.unio.so/v1")
        self.assertEqual(ur.normalize_api("https://api.unio.so/v1/"), "https://api.unio.so/v1")
        self.assertEqual(ur.normalize_api("http://localhost:3355/v1"), "http://localhost:3355/v1")
        for bad in ("http://api.unio.so/v1", "ftp://x", "https://a b", "https://x/v1?q=1", ""):
            with self.assertRaises(ur.Stop):
                ur.normalize_api(bad)

    def test_repository_answers(self):
        self.assertEqual(ur.parse_repos("a/b, https://github.com/c/d.git  e/f,a/b"), (["a/b", "c/d", "e/f"], []))
        self.assertEqual(ur.parse_repos("a, ../b, a/.."), ([], ["a", "../b", "a/.."]))
        self.assertIsNone(ur.normalize_repos({"PLA": ["a/b", 3]}))
        self.assertIsNone(ur.normalize_repos(["a/b"]))
        self.assertEqual(ur.normalize_repos({"PLA": "a/b", "SUP": []}), {"PLA": ["a/b"]})


class Commands(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.config, self.state, self.token_file = self.box.paths()

    def make_profile(self, org="org_test", lines=(), token=True, env=None):
        config, state, token_file = self.box.paths(org)
        config.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        if token:
            token_file.write_text(RUNNER, encoding="utf-8")
        if lines:
            (state / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
        if env:
            (config / "env").write_text(env, encoding="utf-8")

    def test_status_without_profiles(self):
        code, out = self.box.runner(["status"])
        self.assertEqual(code, 0, out)
        self.assertIn("Nenhum serviço de respostas está configurado neste computador.", out)

    def test_status_reads_the_log_and_the_env_file(self):
        self.make_profile(lines=[{"at": "2026-10-23T10:00:00Z", "event": "start", "version": "1.9.0"},
                                 {"at": "2026-10-23T11:00:00Z", "event": "start", "version": "2.0.0"},
                                 {"at": "2026-10-23T11:00:30Z", "event": "idle", "items": 0}],
                          env="# local\nUNIO_RUNNER_AUTOUPDATE = off\n")
        code, out = self.box.runner(["status", "--workspace", "org_test"])
        self.assertEqual(code, 0, out)
        self.assertIn("org_test: sem serviço instalado", out)
        self.assertIn("  versão: 2.0.0", out)
        self.assertIn("  atualização automática: desligada (UNIO_RUNNER_AUTOUPDATE=off)", out)
        self.assertIn("  última linha do registro: 2026-10-23T11:00:30Z idle items=0", out)

    def test_logs(self):
        self.make_profile(lines=[{"at": "t%d" % n, "event": "poll", "listed": n} for n in range(5)])
        with open(self.state / "log.jsonl", "a", encoding="utf-8") as f:
            f.write("not json \x1b[31m\n")
        code, out = self.box.runner(["logs", "--workspace", "org_test", "--lines", "3"])
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertEqual(lines[:3], ["t3 poll listed=3", "t4 poll listed=4", "not json [31m"])
        code, out = self.box.runner(["logs", "--workspace", "org_test", "--lines", "0"])
        self.assertEqual(code, 2, out)

    def test_empty_log(self):
        code, out = self.box.runner(["logs", "--workspace", "org_test"])
        self.assertEqual(code, 0, out)
        self.assertIn("O registro de org_test ainda está vazio", out)

    def test_commands_need_an_installed_service(self):
        for command in ("start", "stop", "restart"):
            code, out = self.box.runner([command, "--workspace", "org_test"])
            self.assertEqual(code, 1, out)
            self.assertIn("Não há serviço instalado para org_test. Rode primeiro:", out)

    def test_uninstall_keeps_the_template_while_another_workspace_uses_it(self):
        self.make_profile("org_test")
        self.make_profile("org_other")
        svc = mock.Mock()
        svc.state.return_value = "running"
        with self.box.inside(), captured() as out, mock.patch.object(ur, "service", lambda kind=None: svc):
            self.assertEqual(ur.main(["uninstall", "--workspace", "org_test"]), 0)
        svc.remove.assert_called_once_with("org_test", last=False)
        self.assertFalse(self.token_file.exists())
        self.assertTrue(self.box.paths("org_other")[2].exists())
        self.assertIn("Token apagado de", out.getvalue())
        with self.box.inside(), captured(), mock.patch.object(ur, "service", lambda kind=None: svc):
            self.assertEqual(ur.main(["uninstall", "--workspace", "org_other"]), 0)
        svc.remove.assert_called_with("org_other", last=True)

    def test_uninstall_without_anything_installed(self):
        code, out = self.box.runner(["uninstall", "--workspace", "org_test"])
        self.assertEqual(code, 0, out)
        self.assertIn("Não havia serviço instalado para org_test.", out)
        self.assertIn("Não havia token do serviço salvo para org_test neste computador.", out)


# An empty file works everywhere; os.devnull is "nul" on Windows.
EMPTY_CONFIG = tempfile.NamedTemporaryFile(prefix="unio-runner-gitconfig-", delete=False)
EMPTY_CONFIG.close()
GIT_ISOLATION = {"GIT_CONFIG_GLOBAL": EMPTY_CONFIG.name, "GIT_CONFIG_NOSYSTEM": "1"}


def tearDownModule():
    os.unlink(EMPTY_CONFIG.name)


def git(*args, cwd=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid", **GIT_ISOLATION)
    done = subprocess.run(["git"] + list(args), cwd=cwd, env=env, capture_output=True, encoding="utf-8")
    if done.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), done.stderr))
    return done.stdout.strip()


class Update(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        git("init", "--quiet", "--bare", "-b", "main", str(root / "origin.git"))
        self.work = root / "work"
        git("clone", "--quiet", str(root / "origin.git"), str(self.work))
        (self.work / "runner").mkdir()
        (self.work / "runner" / "VERSION").write_text("2.0.0\n", encoding="utf-8")
        git("add", "runner/VERSION", cwd=self.work)
        git("commit", "--quiet", "-m", "first", cwd=self.work)
        git("push", "--quiet", "origin", "HEAD:main", cwd=self.work)
        self.clone = root / "clone"
        git("clone", "--quiet", "-b", "main", str(root / "origin.git"), str(self.clone))

    def publish(self, version):
        (self.work / "runner" / "VERSION").write_text(version + "\n", encoding="utf-8")
        git("commit", "--quiet", "-am", version, cwd=self.work)
        git("push", "--quiet", "origin", "HEAD:main", cwd=self.work)

    def update(self, states):
        svc = mock.Mock()
        svc.state.side_effect = lambda org: states[org]
        for org in states:
            config, _, _ = self.box.paths(org)
            config.mkdir(parents=True)
        with self.box.inside(**GIT_ISOLATION), captured() as out, \
                mock.patch.object(ur, "CLONE", self.clone), mock.patch.object(ur, "service", lambda kind=None: svc):
            code = ur.main(["update"])
        return code, out.getvalue(), svc

    def test_pulls_and_restarts_what_was_running(self):
        self.publish("2.0.1")
        code, out, svc = self.update({"org_a": "running", "org_b": "stopped", "org_c": "failed"})
        self.assertEqual(code, 0, out)
        self.assertIn("2.0.0 para 2.0.1", out)
        self.assertEqual((self.clone / "runner" / "VERSION").read_text(encoding="utf-8"), "2.0.1\n")
        self.assertEqual([c.args for c in svc.restart.call_args_list], [("org_a",), ("org_c",)])
        self.assertIn("org_b: parado, continua parado.", out)

    def test_already_current(self):
        code, out, _ = self.update({})
        self.assertEqual(code, 0, out)
        self.assertIn("já estava atualizado", out)

    def test_a_diverged_clone_is_left_alone(self):
        self.publish("2.0.1")
        (self.clone / "runner" / "VERSION").write_text("local\n", encoding="utf-8")
        git("commit", "--quiet", "-am", "local", cwd=self.clone)
        code, out, svc = self.update({"org_a": "running"})
        self.assertEqual(code, 1, out)
        self.assertIn("Não foi possível atualizar", out)
        svc.restart.assert_not_called()

    def test_not_a_clone(self):
        with self.box.inside(), captured() as out, mock.patch.object(ur, "CLONE", self.box.root):
            self.assertEqual(ur.main(["update"]), 1)
        self.assertIn("não é um clone git", out.getvalue())


if __name__ == "__main__":
    unittest.main()
