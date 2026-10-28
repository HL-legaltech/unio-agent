"""unio-runner's arguments and its commands after setup: status, logs,
start/stop/restart, uninstall and update."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inbox_support import inbox, non_utf8_locale
from runner_support import RUNNER, Sandbox, captured, load_runner

ur = load_runner()


class Arguments(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ur.parse_args(["setup", "--workspace", "org_x", "--yes", "--api=https://a.example/v1"]),
                         ("setup", {"--workspace": "org_x", "--yes": True, "--api": "https://a.example/v1"}))
        self.assertEqual(ur.parse_args(["update"]), ("update", {}))
        self.assertEqual(ur.parse_args(["status"]), ("status", {}))
        self.assertEqual(ur.parse_args(["status", "--json"]), ("status", {"--json": True}))
        # --workspace is no longer the parser's to require: one profile stands for it.
        self.assertEqual(ur.parse_args(["start"]), ("start", {}))
        self.assertEqual(ur.parse_args(["logs", "-n", "5", "-f"]), ("logs", {"--lines": "5", "--follow": True}))
        self.assertEqual(ur.parse_args(["logs", "-n=7"]), ("logs", {"--lines": "7"}))
        self.assertEqual(ur.parse_args(["uninstall", "--yes"]), ("uninstall", {"--yes": True}))
        for argv in ([], ["-h"], ["--help"], ["help"], ["logs", "--help"]):
            self.assertEqual(ur.parse_args(argv)[0], "help", argv)

    def test_refusals_are_portuguese_and_exit_2(self):
        for argv, text in ((["bogus"], "Comando desconhecido: bogus. Rode unio runner help."),
                           (["setup", "--workspace", "org_x", "--token", RUNNER], "Opção desconhecida para setup: --token."),
                           (["logs", "--workspace"], "Falta o valor de --workspace."),
                           (["logs", "-n"], "Falta o valor de -n."),
                           (["logs", "-f=1"], "-f não leva valor."),
                           (["status", "-n", "3"], "Opção desconhecida para status: -n."),
                           (["start", "-f"], "Opção desconhecida para start: -f."),
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
        self.assertTrue(text.startswith("Uso: unio runner <comando> [opções]"))
        for command in ("setup", "status", "start", "stop", "restart", "logs", "uninstall", "update"):
            self.assertIn("  " + command, text)
        self.assertNotIn("unio-runner", text)

    def test_help_into_a_pipe_is_utf8(self):
        done = subprocess.run([sys.executable, str(ur.HERE / "unio-runner"), "--help"], stdin=subprocess.DEVNULL,
                              capture_output=True, env=non_utf8_locale(os.environ), timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.decode("utf-8", "replace").replace("\r\n", "\n"), ur.USAGE)

    def test_people_are_told_unio_runner(self):
        # unio-runner survives only as a name that is not a command: the
        # systemd unit and its template, the task's template, the User-Agent.
        source = ur.HERE.joinpath("unio-runner").read_text(encoding="utf-8")
        named = [line.strip() for line in source.splitlines() if re.search(r"unio-runner(?![@-])", line)]
        self.assertEqual(named, ['"Authorization": "Bearer " + token, "Accept": "application/json", '
                                 '"User-Agent": "unio-runner"})'])
        for template in ur.SERVICES.iterdir():
            self.assertIsNone(re.search(r"unio-runner (setup|status|start|stop|restart|logs|uninstall|update)",
                                        template.read_text(encoding="utf-8")), template.name)

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

    def test_status_shows_the_research_limits(self):
        self.make_profile(lines=[{"at": "2026-10-23T11:00:00Z", "event": "start", "version": "2.1.0"}])
        research = {"web": "on", "protocol": True,
                    "configError": "UNIO_INBOX_RESEARCH_MAX_BUDGET_USD=0.01 is not a number from 0.05 to 50",
                    "caps": {"turns": 60, "usd": 3.0, "runsPerDay": 8, "reserveUsd": 9.5, "timeoutS": 780,
                             "quotaGate": 0.9}}
        (self.state / "health.json").write_text(json.dumps({"research": research}), encoding="utf-8")
        code, out = self.box.runner(["status", "--workspace", "org_test"])
        self.assertEqual(code, 0, out)
        self.assertIn("  pesquisa: ligada, busca e páginas", out)
        self.assertIn("  limites da pesquisa: 8 por dia, 60 turnos e US$ 3 cada; para com US$ 9,5 gastos em 24 h, "
                      "e sem web com 90% da semana usados", out)
        self.assertIn("  configuração da pesquisa ignorada, valem os padrões: UNIO_INBOX_RESEARCH_MAX_BUDGET_USD=0.01 "
                      "is not a number from 0.05 to 50", out)
        code, out = self.box.runner(["status", "--json", "--workspace", "org_test"])
        self.assertEqual(json.loads(out)["workspaces"][0]["research"], research)

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
            self.assertEqual(ur.main(["uninstall", "--workspace", "org_test", "--yes"]), 0)
        svc.remove.assert_called_once_with("org_test", last=False)
        self.assertFalse(self.token_file.exists())
        self.assertTrue(self.box.paths("org_other")[2].exists())
        self.assertIn("Token apagado de", out.getvalue())
        # One token left: the command needs no --workspace now.
        with self.box.inside(), captured(), mock.patch.object(ur, "service", lambda kind=None: svc):
            self.assertEqual(ur.main(["uninstall", "--yes"]), 0)
        svc.remove.assert_called_with("org_other", last=True)

    def test_uninstall_without_anything_installed(self):
        # Nothing to remove, nothing to confirm.
        code, out = self.box.runner(["uninstall", "--workspace", "org_test"])
        self.assertEqual(code, 0, out)
        self.assertIn("Não havia serviço instalado para org_test.", out)
        self.assertIn("Não havia token do serviço salvo para org_test neste computador.", out)

    def uninstall_asking(self, answer, tty=True):
        svc = mock.Mock()
        svc.state.return_value = "running"
        asked = []

        def prompt(text):
            asked.append(text)
            return answer

        with self.box.inside(), captured() as out, mock.patch.object(ur, "service", lambda kind=None: svc), \
                mock.patch.object(ur, "prompt", prompt), mock.patch.object(ur, "stdin_is_tty", lambda: tty):
            code = ur.main(["uninstall", "--workspace", "org_test"])
        return code, out.getvalue(), asked, svc

    def test_uninstall_asks_first(self):
        self.make_profile()
        code, out, asked, svc = self.uninstall_asking("")
        self.assertEqual(code, 1, out)
        self.assertEqual(asked, ["Remover o serviço de respostas de org_test deste computador e apagar o token "
                                 "dele? [s/N] "])
        self.assertIn("Nada foi removido.", out)
        svc.remove.assert_not_called()
        self.assertTrue(self.token_file.exists())
        code, out, _, svc = self.uninstall_asking("s")
        self.assertEqual(code, 0, out)
        svc.remove.assert_called_once()
        self.assertFalse(self.token_file.exists())

    def test_uninstall_without_a_terminal_needs_yes(self):
        self.make_profile()
        code, out, asked, svc = self.uninstall_asking("s", tty=False)
        self.assertEqual(code, 2, out)
        self.assertIn("rode de novo com --yes", out)
        self.assertEqual(asked, [])
        svc.remove.assert_not_called()
        self.assertTrue(self.token_file.exists())

    def test_the_only_workspace_needs_no_flag(self):
        self.make_profile(lines=[{"at": "t1", "event": "poll", "listed": 1}])
        # What uninstall leaves, a profile without a token, is not a choice.
        self.make_profile("org_gone", token=False)
        code, out = self.box.runner(["logs"])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "t1 poll listed=1")

    def test_several_workspaces_or_none_are_named(self):
        code, out = self.box.runner(["logs"])
        self.assertEqual(code, 2, out)
        self.assertIn("Falta --workspace org_...: nenhum serviço de respostas está configurado neste computador.", out)
        self.make_profile("org_one")
        self.make_profile("org_two")
        for command in ("setup", "start", "stop", "restart", "logs", "uninstall"):
            code, out = self.box.runner([command])
            self.assertEqual(code, 2, out)
            self.assertIn("mais de um workspace: org_one, org_two. Diga qual com --workspace.", out)

    def test_status_json(self):
        self.make_profile(lines=[{"at": "2026-10-23T11:00:00Z", "event": "start", "version": "2.0.0"},
                                 {"at": "2026-10-23T11:00:30Z", "event": "idle", "items": 0}],
                          env="UNIO_RUNNER_AUTOUPDATE=off\n")
        self.make_profile("org_gone", token=False)
        code, out = self.box.runner(["status", "--json"])
        self.assertEqual(code, 0, out)
        # Without an env file, a runner updates itself when its clone is a git clone.
        in_clone = (ur.CLONE / ".git").exists()
        self.assertEqual(json.loads(out), {"workspaces": [
            {"workspace": "org_gone", "state": "absent", "version": None, "autoUpdate": in_clone, "token": False,
             "lastLog": None, "projectStatus": None, "research": None},
            {"workspace": "org_test", "state": "absent", "version": "2.0.0", "autoUpdate": False, "token": True,
             "lastLog": {"at": "2026-10-23T11:00:30Z", "event": "idle", "items": 0}, "projectStatus": None,
             "research": None}]})
        code, out = self.box.runner(["status", "--json", "--workspace", "org_test"])
        self.assertEqual([w["workspace"] for w in json.loads(out)["workspaces"]], ["org_test"])

    def test_status_names_the_project_status_pass(self):
        self.make_profile()
        health = self.state / "health.json"
        cases = [
            (None, "status dos projetos: sem informação ainda"),
            ({"mode": "off"}, "status dos projetos: desligado (UNIO_STATUS=on no arquivo env do perfil liga)"),
            ({"mode": "observe", "lastPassAt": "2026-10-27T12:00:00Z", "pending": 0},
             "status dos projetos: só registra, sem mudar nenhum projeto (UNIO_STATUS=on liga); "
             "última passagem em 2026-10-27T12:00:00Z, nenhum projeto esperando"),
            ({"mode": "on", "lastPassAt": "2026-10-27T12:00:00Z", "pending": 2},
             "status dos projetos: ligado; última passagem em 2026-10-27T12:00:00Z, 2 projetos esperando"),
            ({"mode": "on", "lastPassAt": "2026-10-27T12:00:00Z", "lastPassError": "unio_unavailable: 503",
              "lastPassErrorAt": "2026-10-27T12:00:00Z"},
             "status dos projetos: ligado; a última passagem falhou em 2026-10-27T12:00:00Z (o motivo está em "),
            ({"mode": "off", "configError": "UNIO_STATUS_INTERVAL is not a number"},
             "status dos projetos: parado por um valor inválido de UNIO_STATUS, UNIO_STATUS_INTERVAL ou "
             "UNIO_STATUS_DONE_AFTER no arquivo env do perfil"),
        ]
        for block, line in cases:
            with self.subTest(block=block):
                if block is None:
                    health.unlink(missing_ok=True)
                else:
                    health.write_text(json.dumps({"lastPollAt": "2026-10-27T12:00:00Z", "status": block}),
                                      encoding="utf-8")
                code, out = self.box.runner(["status", "--workspace", "org_test"])
                self.assertEqual(code, 0, out)
                self.assertIn("  " + line, out)
        code, out = self.box.runner(["status", "--json", "--workspace", "org_test"])
        self.assertEqual(json.loads(out)["workspaces"][0]["projectStatus"],
                         {"mode": "off", "configError": "UNIO_STATUS_INTERVAL is not a number"})

    def test_status_json_without_profiles(self):
        code, out = self.box.runner(["status", "--json"])
        self.assertEqual((code, json.loads(out)), (0, {"workspaces": []}))

    def test_logs_follow(self):
        self.make_profile(lines=[{"at": "t0", "event": "poll"}])
        log = self.state / "log.jsonl"
        naps = []

        def nap(seconds):
            # The first nap sees a new line come in; the second is Ctrl-C.
            naps.append(seconds)
            if len(naps) > 1:
                raise KeyboardInterrupt
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": "t1", "event": "claimed"}) + "\n")

        with self.box.inside(), captured() as out, mock.patch.object(ur.time, "sleep", nap):
            self.assertEqual(ur.main(["logs", "-f", "-n", "1"]), 0)
        self.assertEqual(out.getvalue().splitlines()[:2], ["t0 poll", "t1 claimed"])


class EnvFile(unittest.TestCase):
    """A profile's env file means the same to unio-runner as to unio-inbox."""

    def test_reads_what_the_runner_reads(self):
        box = Sandbox(self)
        path = box.root / "env"
        prof = ur.Profile("org_test", box.root, box.root, box.root / "runner-token")
        for text in ('\ufeffUNIO_API_URL="http://127.0.0.1:1/v1"\nUNIO_RUNNER_AUTOUPDATE=\'off\'\n',
                     "# local\n UNIO_RUNNER_AUTOUPDATE = false \nUNIO_API_URL=https://api.unio.so/v1\n",
                     "UNIO_RUNNER_AUTOUPDATE=0\n", "UNIO_RUNNER_AUTOUPDATE=No\n", "UNIO_RUNNER_AUTOUPDATE=on\n", ""):
            path.write_text(text, encoding="utf-8")
            ours, theirs = ur.read_env_file(path), inbox.read_env_file(path)
            for key in ("UNIO_API_URL", "UNIO_RUNNER_AUTOUPDATE"):
                self.assertEqual(ours.get(key), theirs.get(key), text)
            with box.inside():
                off = ur.autoupdate(prof).startswith("desligada (UNIO_RUNNER_AUTOUPDATE=")
            self.assertEqual(off, not inbox.autoupdate_on(theirs), text)


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

    def update(self, states, self_updating=()):
        """`update` with these services; those in self_updating update themselves
        and run 2.0.0, the others have UNIO_RUNNER_AUTOUPDATE=off."""
        svc = mock.Mock()
        svc.state.side_effect = lambda org: states[org]
        for org in states:
            config, state, _ = self.box.paths(org)
            config.mkdir(parents=True)
            state.mkdir(parents=True)
            (state / "log.jsonl").write_text(json.dumps({"event": "start", "version": "2.0.0"}) + "\n",
                                             encoding="utf-8")
            if org not in self_updating:
                (config / "env").write_text("UNIO_RUNNER_AUTOUPDATE=off\n", encoding="utf-8")
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

    def test_a_self_updating_service_is_not_cut_off(self):
        # It restarts on its own between items once the clone holds a newer runner/VERSION.
        self.publish("2.0.1")
        code, out, svc = self.update({"org_a": "running", "org_b": "running", "org_c": "failed"},
                                     self_updating=("org_a", "org_c"))
        self.assertEqual(code, 0, out)
        self.assertEqual([c.args for c in svc.restart.call_args_list], [("org_b",), ("org_c",)])
        self.assertIn("org_a: passa sozinho para a versão 2.0.1 quando terminar o que está fazendo.", out)

    def test_a_self_updating_service_on_the_same_version(self):
        code, out, svc = self.update({"org_a": "running"}, self_updating=("org_a",))
        self.assertEqual(code, 0, out)
        svc.restart.assert_not_called()
        self.assertIn("org_a: segue ligado na versão 2.0.0. Para reiniciar agora:", out)
        self.assertIn("restart --workspace org_a", out)

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
