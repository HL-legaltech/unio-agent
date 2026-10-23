"""unio-runner setup, run as a person runs it against the real unio-inbox
and the fakes in runner_support.py: the checks, the token, unio-inbox
--check, the repositories and, on this system, the service."""

import json
import os
import sys
import unittest
from unittest import mock

from runner_support import (AGENT, BARRED, OFF, OTHER, REVOKED, RUNNER, RUNNER_DIR, SESSION, TOKENS, WORKSPACE,
                            FakeApi, Sandbox, captured, closed_port_url, load_runner)

ur = load_runner()
VERSION = (RUNNER_DIR / "VERSION").read_text(encoding="utf-8").strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.api = FakeApi()
        self.addCleanup(self.api.close)
        self.config, self.state, self.token_file = self.box.paths()
        self.repos = self.box.root / "repos.json"
        self.repos.write_text(json.dumps({"PLA": ["octo-org/api"], "SUP": "octo-org/help"}), encoding="utf-8")

    def setup(self, *args, token=RUNNER, **env):
        """setup without a service, the token piped in."""
        argv = ["setup", "--workspace", "org_test", "--api", self.api.url, "--token-stdin", "--no-service"]
        return self.box.runner(argv + list(args), input_text=token + "\n", **env)

    def token(self):
        try:
            return self.token_file.read_text(encoding="utf-8")
        except OSError:
            return None

    def assertNoTokenLeaked(self, *outputs):
        files = (self.box.claude_log, self.box.service_log, self.state / "log.jsonl")
        texts = list(outputs) + [p.read_text(encoding="utf-8") for p in files if p.exists()]
        texts += [path for _, path in self.api.requests]
        for text in texts:
            for token in TOKENS:
                self.assertNotIn(token, text)
            self.assertNotIn("unio_", text)


class SetupWithoutService(Base):
    def test_saves_the_token_the_repositories_and_the_api(self):
        code, out = self.setup("--repos-json", str(self.repos))
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[-1], "Perfil do serviço de respostas pronto para %s." % WORKSPACE)
        self.assertIn("Claude Code 2.1.309 encontrado.", out)
        self.assertIn("Claude Code conectado a uma conta.", out)
        self.assertIn("Token aceito: %s, no workspace %s." % (AGENT, WORKSPACE), out)
        self.assertEqual(self.token(), RUNNER)
        if os.name != "nt":
            self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o700)
            # The runner's log, health and runs go here, whatever the terminal's umask.
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((self.config / "repos.json").read_text(encoding="utf-8")),
                         {"PLA": ["octo-org/api"], "SUP": ["octo-org/help"]})
        self.assertIn("UNIO_API_URL=%s" % self.api.url, (self.config / "env").read_text(encoding="utf-8").splitlines())
        self.assertEqual([c for c in self.box.logged(self.box.claude_log)], [["--version"], ["auth", "status", "--json"]])
        # The workspace and agent names above came from unio-inbox --check, its only request.
        self.assertEqual(self.api.requests, [("GET", "/v1/agents/me")])
        self.assertEqual(list(self.config.glob("*.new")), [])
        self.assertNoTokenLeaked(out)

    def test_each_refusal_says_why_and_saves_nothing(self):
        for token, reason in ((REVOKED, "O token foi recusado"), (OFF, "Os agentes estão desligados neste workspace"),
                              (BARRED, "O Unio recusou o agente com este token."),
                              (SESSION, "Este não é um token do serviço de respostas"),
                              (OTHER, "Este token é do workspace org_Other, mas o comando é para o org_test.")):
            code, out = self.setup("--yes", token=token)
            self.assertEqual(code, 1, out)
            self.assertIn("O Unio não aceitou este token para o serviço de respostas. Nada foi salvo.", out)
            self.assertIn(reason, out)
            if token != OFF:
                self.assertNotIn("Os agentes estão desligados", out)
            self.assertNotIn("O que a verificação encontrou", out)
            self.assertIsNone(self.token(), token)
            self.assertFalse((self.config / "repos.json").exists())
            self.assertNoTokenLeaked(out)

    def test_a_runner_without_claude_code_saves_nothing(self):
        code, out = self.setup("--yes", UNIO_INBOX_CLAUDE=str(self.box.root / "no-claude"))
        self.assertEqual(code, 1, out)
        self.assertIn("O Unio aceitou o token, mas o serviço não consegue rodar neste computador. Nada foi salvo.", out)
        self.assertIn("O serviço não encontrou o Claude Code.", out)
        self.assertIsNone(self.token())

    def test_a_problem_without_a_known_code_shows_as_written(self):
        check = {"ok": False, "problems": ["a new problem", "Unio did not answer"],
                 "codes": ["new_code", "unio_unreachable"]}
        prof = ur.Profile("org_test", self.config, self.state, self.token_file)
        text = ur.check_failure("org_test", check, prof)
        self.assertEqual(text.splitlines(), [
            "O Unio não aceitou este token para o serviço de respostas. Nada foi salvo.",
            "O Unio não respondeu: confira a rede e rode o comando de novo.",
            "O que a verificação encontrou:", "  - a new problem", "  - Unio did not answer"])
        self.assertIn("A verificação não disse o motivo.", ur.check_failure("org_test", {"ok": False}, prof))

    def test_an_untrusted_certificate_names_the_fix(self):
        # python.org's Python on a Mac before Install Certificates, or a network that inspects HTTPS.
        error = ("https://api.unio.so/v1 unreachable: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                 "unable to get local issuer certificate (_ssl.c:1006)")
        prof = ur.Profile("org_test", self.config, self.state, self.token_file)
        lines = ur.check_failure("org_test", {"ok": False, "problems": [error], "codes": ["tls_untrusted"]},
                                 prof).splitlines()
        self.assertEqual(lines[0], "Não foi possível conferir o token com o Unio. Nada foi salvo.")
        self.assertIn("Install Certificates.command", lines[1])
        self.assertNotIn("confira a rede", "\n".join(lines))
        self.assertEqual(lines[2:], ["O que a verificação encontrou:", "  - " + error[:200]])

    def test_a_refused_token_keeps_the_one_saved_before(self):
        self.assertEqual(self.setup("--yes")[0], 0)
        code, out = self.setup("--yes", token=REVOKED)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.token(), RUNNER)

    def test_no_answer_from_unio(self):
        code, out = self.box.runner(["setup", "--workspace", "org_test", "--api", closed_port_url(), "--token-stdin",
                                     "--no-service", "--yes"], input_text=RUNNER + "\n")
        self.assertEqual(code, 1, out)
        self.assertIn("Não foi possível conferir o token com o Unio. Nada foi salvo.", out)
        self.assertIn("O Unio não respondeu: confira a rede", out)
        # What the network said, which the reason alone would hide.
        self.assertIn("/v1 unreachable: ", out)
        self.assertIsNone(self.token())
        self.assertFalse((self.config / "env").exists())

    def test_a_malformed_token_is_never_sent(self):
        code, out = self.setup("--yes", token="unio_short")
        self.assertEqual(code, 1, out)
        self.assertIn("Isso não é um token do Unio (unio_ seguido de 46 letras e números). Nada foi salvo.", out)
        self.assertEqual(self.api.requests, [])
        self.assertIsNone(self.token())

    def test_claude_signed_out_stops_before_the_token(self):
        code, out = self.setup("--yes", FAKE_CLAUDE_AUTH="out")
        self.assertEqual(code, 1, out)
        self.assertIn("não está conectado a uma conta", out)
        self.assertEqual(self.api.requests, [])
        self.assertFalse(self.config.exists())

    def test_a_cli_without_auth_status_only_warns(self):
        code, out = self.setup("--yes", FAKE_CLAUDE_AUTH="old")
        self.assertEqual(code, 0, out)
        self.assertIn("Não foi possível confirmar que o Claude Code está conectado a uma conta", out)

    def test_refuses_inside_claude_code(self):
        code, out = self.setup("--yes", CLAUDECODE="1")
        self.assertEqual(code, 1, out)
        self.assertIn("fora do Claude Code", out)
        self.assertEqual(self.box.logged(self.box.claude_log), [])

    def test_missing_git_or_claude(self):
        for missing, text in (("git", "O comando git não está no PATH."),
                              ("claude", "O comando claude não está no PATH.")):
            which = lambda name, *a, **k: None if name == missing else "/usr/bin/" + name
            with self.box.inside(), mock.patch.object(ur.shutil, "which", which):
                with self.assertRaises(ur.Stop) as caught:
                    ur.check_computer()
            self.assertIn(text, caught.exception.message)

    def test_token_stdin_needs_the_repositories_or_yes(self):
        code, out = self.box.runner(["setup", "--workspace", "org_test", "--token-stdin"], input_text=RUNNER)
        self.assertEqual(code, 2, out)
        self.assertIn("passe também --repos-json ou --yes", out)

    def test_bad_arguments(self):
        for args, text in ((["--api", "ftp://example"], "O valor de --api"),
                           (["--workspace", "org_a-b"], "não é um workspace do Unio")):
            argv = ["setup", "--workspace", "org_test", "--yes", "--no-service"] + args
            code, out = self.box.runner(argv)
            self.assertEqual(code, 2, out)
            self.assertIn(text, out)

    def test_bad_repositories_file_stops_before_anything(self):
        self.repos.write_text(json.dumps({"PLA": ["not a repo"]}), encoding="utf-8")
        code, out = self.setup("--repos-json", str(self.repos))
        self.assertEqual(code, 2, out)
        self.assertIn("O arquivo de --repos-json precisa ser um objeto JSON", out)
        self.assertEqual(self.api.requests, [])
        self.assertEqual(self.box.logged(self.box.claude_log), [])

    def test_yes_keeps_the_saved_token_and_repositories(self):
        self.assertEqual(self.setup("--repos-json", str(self.repos))[0], 0)
        code, out = self.box.runner(["setup", "--workspace", "org_test", "--no-service", "--yes"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("Cole o token", out)
        self.assertEqual(self.token(), RUNNER)
        self.assertEqual(json.loads((self.config / "repos.json").read_text(encoding="utf-8")),
                         {"PLA": ["octo-org/api"], "SUP": ["octo-org/help"]})

    def test_without_a_terminal_and_without_token_stdin(self):
        code, out = self.box.runner(["setup", "--workspace", "org_test", "--no-service", "--yes"])
        self.assertEqual(code, 1, out)
        self.assertIn("precisa de um terminal para ler o token sem mostrá-lo, ou de --token-stdin", out)


class Questions(Base):
    """The interactive steps, in this process, with the person's answers."""

    def run_setup(self, answers, secret=RUNNER, args=()):
        asked = []

        def prompt(text):
            asked.append(text)
            return answers.pop(0) if answers else None

        secrets = []

        def prompt_secret(text):
            secrets.append(text)
            return secret

        argv = ["setup", "--workspace", "org_test", "--api", self.api.url, "--no-service"] + list(args)
        with self.box.inside(), captured() as out, mock.patch.object(ur, "prompt", prompt), \
                mock.patch.object(ur, "prompt_secret", prompt_secret), mock.patch.object(ur, "stdin_is_tty", lambda: True):
            code = ur.main(argv)
        return code, out.getvalue(), asked, secrets

    def saved_repos(self):
        return json.loads((self.config / "repos.json").read_text(encoding="utf-8"))

    def test_asks_each_team_until_the_answer_is_valid(self):
        code, out, asked, secrets = self.run_setup(
            ["octo-org/api, https://github.com/octo-org/web.git", "not a repo", "octo-org/help octo-org/help"])
        self.assertEqual(code, 0, out)
        self.assertEqual(secrets, ["Cole o token do serviço de respostas: "])
        self.assertEqual(asked, ["Time Platform (PLA) [Enter para nenhum]: ", "Time Support (SUP) [Enter para nenhum]: ",
                                 "Time Support (SUP) [Enter para nenhum]: "])
        self.assertIn("not, a, repo: use o formato dono/nome", out)
        self.assertEqual(self.saved_repos(), {"PLA": ["octo-org/api", "octo-org/web"], "SUP": ["octo-org/help"]})
        self.assertNoTokenLeaked(out)

    def test_enter_keeps_a_dash_clears_and_other_teams_stay(self):
        self.config.mkdir(parents=True)
        (self.config / "repos.json").write_text(json.dumps({"PLA": ["a/b"], "SUP": ["c/d"], "OLD": ["e/f"]}),
                                                encoding="utf-8")
        code, out, asked, _ = self.run_setup(["", "-"])
        self.assertEqual(code, 0, out)
        self.assertEqual(asked[0], "Time Platform (PLA) [Enter mantém a/b; - para nenhum]: ")
        self.assertEqual(self.saved_repos(), {"PLA": ["a/b"], "OLD": ["e/f"]})

    def test_a_saved_token_is_replaced_only_when_asked(self):
        self.assertEqual(self.run_setup([""] * 2)[0], 0)
        code, out, asked, secrets = self.run_setup(["", "", ""], secret=OTHER)
        self.assertEqual(code, 0, out)
        self.assertTrue(asked[0].startswith("Já existe um token do serviço para org_test neste computador."))
        self.assertEqual(secrets, [])
        self.assertEqual(self.token(), RUNNER)
        code, out, _, secrets = self.run_setup(["s"], secret=OTHER)
        self.assertEqual(code, 1, out)
        self.assertEqual(len(secrets), 1)
        self.assertIn("Este token é do workspace org_Other", out)
        self.assertEqual(self.token(), RUNNER)

    def test_ctrl_c_at_the_prompt_saves_nothing(self):
        def interrupted(text):
            raise KeyboardInterrupt

        with self.box.inside(), captured() as out, mock.patch.object(ur, "prompt_secret", interrupted), \
                mock.patch.object(ur, "stdin_is_tty", lambda: True):
            code = ur.main(["setup", "--workspace", "org_test", "--api", self.api.url, "--no-service"])
        self.assertEqual(code, 130)
        self.assertIn("Cancelado.", out.getvalue())
        self.assertIsNone(self.token())


class FakeService:
    """A service manager that starts the runner (or not) when installed."""

    kind = "fake"

    def __init__(self, box, starts=True, available=True):
        self.box, self.starts, self.is_available = box, starts, available
        self.installed = []

    def name(self, org):
        return "fake@" + org

    def available(self):
        return self.is_available

    def unavailable(self, org):
        return "no service manager here"

    def install(self, org, prof, python, inbox, path, env=None):
        self.installed.append((org, python, str(inbox), path))
        self.env = env
        if self.starts:
            prof.state.mkdir(parents=True, exist_ok=True)
            with open(prof.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": "2026-10-23T12:00:00Z", "event": "start", "version": "2.0.0"}) + "\n")

    def hints(self, user=None):
        return ["a hint from the service manager"]


class SetupWithService(Base):
    def run_setup(self, svc, **env):
        argv = ["setup", "--workspace", "org_test", "--api", self.api.url, "--token-stdin", "--yes"]
        with self.box.inside(**env), captured() as out, mock.patch.object(ur, "service", lambda kind=None: svc), \
                mock.patch.object(ur, "service_kind", lambda platform=None: "fake"), \
                mock.patch.object(sys, "stdin", mock.Mock(read=lambda: RUNNER + "\n")):
            code = ur.main(argv)
        return code, out.getvalue()

    def test_waits_for_the_runner_to_start(self):
        svc = FakeService(self.box)
        code, out = self.run_setup(svc)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[-1], "Serviço de respostas ligado para %s." % WORKSPACE)
        self.assertIn("a hint from the service manager", out)
        self.assertEqual(svc.installed, [("org_test", sys.executable, str(self.box.inbox), ur.service_path(
            dict(os.environ, PATH=self.box.env()["PATH"])))])
        self.assertEqual(svc.env, {})

    def test_the_service_gets_the_xdg_folders_setup_used(self):
        # Setup saved the profile under them; without them the service would look in ~/.config.
        folders = {"XDG_CONFIG_HOME": str(self.box.root / "xdg config"), "XDG_STATE_HOME": str(self.box.root / "xdg-state")}
        svc = FakeService(self.box)
        code, out = self.run_setup(svc, **folders)
        self.assertEqual(code, 0, out)
        self.assertTrue((self.box.root / "xdg config" / "unio" / "runners" / "org_test" / "runner-token").exists())
        self.assertEqual(svc.env, {} if os.name == "nt" else folders)

    def test_a_runner_that_never_starts_is_reported(self):
        self.state.mkdir(parents=True)
        (self.state / "log.jsonl").write_text(json.dumps({"at": "2026-10-22T00:00:00Z", "event": "start"}) + "\n",
                                              encoding="utf-8")
        with mock.patch.object(ur, "START_WAIT", 0):
            code, out = self.run_setup(FakeService(self.box, starts=False))
        self.assertEqual(code, 1, out)
        self.assertIn("O serviço foi instalado, mas não deu sinal em 0 s.", out)
        self.assertIn("logs --workspace org_test", out)

    def test_no_service_manager(self):
        code, out = self.run_setup(FakeService(self.box, available=False))
        self.assertEqual(code, 1, out)
        self.assertIn("no service manager here", out)


class TaskEndToEnd(Base):
    """The Windows task's stop path on every system: the fake Task Scheduler
    runs the real unio-inbox, which stops on its stop file and releases what
    it holds, so /End, which would kill it, is never needed."""

    def test_restart_and_stop_let_the_runner_finish(self):
        self.assertEqual(self.setup("--repos-json", str(self.repos))[0], 0)
        with self.box.inside():
            prof = ur.profile("org_test")
            svc = ur.Tasks(user="PC\\someone")
            with captured():
                svc.install("org_test", prof, sys.executable, self.box.inbox, "")
                self.assertEqual(svc.state("org_test"), "running")
                svc.restart("org_test")
                self.assertEqual(svc.state("org_test"), "running")
                svc.stop("org_test")
                self.assertEqual(svc.state("org_test"), "stopped")
        events = self.box.logged(prof.log_file)
        self.assertEqual([e["event"] for e in events if e["event"] in ("start", "stop")],
                         ["start", "stop", "start", "stop"])
        self.assertEqual([e.get("by") for e in events if e["event"] == "stop"], ["stop_file", "stop_file"])
        self.assertNotIn("/End", [argv[1] for argv in self.box.logged(self.box.service_log) if argv[0] == "schtasks"])
        self.assertFalse((prof.state / ur.STOP_FILE).exists())


@unittest.skipUnless(ur.service_kind() is not None, "no service manager unio-runner knows on this system")
class ServiceEndToEnd(Base):
    """Setup and every command after it, with this system's own service
    manager faked on PATH."""

    def test_setup_and_manage_the_service(self):
        code, out = self.box.runner(["setup", "--workspace", "org_test", "--api", self.api.url, "--token-stdin",
                                     "--repos-json", str(self.repos)], input_text=RUNNER + "\n")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[-1], "Serviço de respostas ligado para %s." % WORKSPACE)
        calls = self.box.logged(self.box.service_log)
        kind = ur.service_kind()
        if kind == "systemd":
            self.assertIn(["systemctl", "--user", "restart", "unio-runner@org_test.service"], calls)
            unit = (self.box.home / ".config" / "systemd" / "user" / "unio-runner@.service").read_text(encoding="utf-8")
            self.assertIn('--workspace %i', unit)
            self.assertIn("loginctl enable-linger", out)
        elif kind == "launchd":
            plist = self.box.home / "Library" / "LaunchAgents" / "so.unio.runner.org_test.plist"
            self.assertIn(["plutil", "-lint", str(plist)], calls)
            self.assertIn(["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(plist)], calls)
        else:
            self.assertIn(["schtasks", "/Create", "/TN", "Unio Runner org_test", "/XML",
                           str(self.state / "task.xml"), "/F"], calls)
        for argv in calls:
            self.assertNotIn("unio-inbox.service", " ".join(argv))

        code, out = self.box.runner(["status"])
        self.assertEqual(code, 0, out)
        self.assertIn("org_test: ligado", out)
        self.assertIn("  versão: %s" % VERSION, out)
        self.assertIn("  atualização automática: ", out)

        self.assertEqual(self.box.runner(["stop", "--workspace", "org_test"])[0], 0)
        self.assertIn("org_test: parado", self.box.runner(["status", "--workspace", "org_test"])[1])
        self.assertEqual(self.box.runner(["start", "--workspace", "org_test"])[0], 0)
        code, out = self.box.runner(["status", "--json"])
        self.assertEqual(code, 0, out)
        self.assertEqual([(w["workspace"], w["state"], w["version"]) for w in json.loads(out)["workspaces"]],
                         [("org_test", "running", VERSION)])
        # The only workspace on this computer needs no --workspace.
        code, out = self.box.runner(["restart"])
        self.assertEqual(code, 0, out)
        self.assertIn("Serviço de org_test reiniciado.", out)

        # The real runner logged a start for setup, start and restart, from the profile's env file.
        code, out = self.box.runner(["logs", "--workspace", "org_test", "-n", "1000"])
        self.assertEqual(code, 0, out)
        starts = [line for line in out.splitlines() if " start " in line]
        self.assertEqual(len(starts), 3, out)
        for line in starts:
            self.assertIn("version=%s" % VERSION, line)
            self.assertIn("workspace=org_test", line)
            self.assertIn("api=%s" % self.api.url, line)

        code, out = self.box.runner(["uninstall", "--workspace", "org_test"])
        self.assertEqual(code, 2, out)
        self.assertIn("org_test: ligado", self.box.runner(["status"])[1])
        code, out = self.box.runner(["uninstall", "--workspace", "org_test", "--yes"])
        self.assertEqual(code, 0, out)
        self.assertIn("Serviço de org_test removido.", out)
        self.assertIn("revogue-o em Configurações > Sistema > Agentes", out)
        self.assertIsNone(self.token())
        self.assertTrue((self.config / "repos.json").exists())
        self.assertIn("org_test: sem serviço instalado", self.box.runner(["status"])[1])
        self.assertNoTokenLeaked(out)


if __name__ == "__main__":
    unittest.main()
