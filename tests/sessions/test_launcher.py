"""session-status.sh, the hook Claude Code runs, and the doctor's line about
it: Python found or not, and the token handed over only as a file path,
resolved per folder as the MCP header helper resolves it."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from support import LAUNCHER, SCRIPTS, WINDOWS, base_env, posix_path, presence_id, remove_tree, wait_for
import fake_api

ON_WINDOWS = "tests/e2e/claude_session.py runs it on Windows, under Git Bash as Claude Code does"


OTHER = "unio_" + ("OtherWorkspaceFakeTokenNotASecret" * 2)[:46]
# What the launcher and lib.sh call, besides a Python.
TOOLS = ("sh", "dirname", "uname", "head", "tr", "sed", "cat", "mkdir", "rm", "mv", "env", "awk", "find", "date",
         "ps", "printf", "grep", "cut", "wc", "sleep", "mktemp", "curl", "chmod", "ls", "lsof", "tail", "sort")


@unittest.skipIf(WINDOWS, ON_WINDOWS)
class Launcher(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-launcher-test-"))
        self.addCleanup(remove_tree, self.tmp)
        self.api = fake_api.FakeApi()
        self.api.start()
        self.addCleanup(self.api.stop)
        self.conf = self.tmp / ".config" / "unio"
        self.conf.mkdir(parents=True)
        (self.conf / "api-url").write_text(self.api.base[:-len("/v1")] + "\n")
        self.session = str(uuid.uuid4())
        self.sessions = self.tmp / "state" / "unio" / "sessions"
        self.alpha = self.tmp / "work" / "alpha"
        self.beta = self.tmp / "work" / "beta"
        for folder in (self.alpha, self.beta):
            folder.mkdir(parents=True)

    def workspace(self, org, token):
        folder = self.conf / "workspaces" / org
        folder.mkdir(parents=True)
        (folder / "token").write_text(token)

    def env(self, **extra):
        return base_env(self.tmp, **extra)

    def run_hook(self, folder, event="SessionStart", env=None, **fields):
        data = {"session_id": self.session, "hook_event_name": event, "cwd": str(folder), **fields}
        env = env or self.env()
        env["CLAUDE_PROJECT_DIR"] = str(folder)
        started = time.monotonic()
        p = subprocess.run(["sh", str(LAUNCHER)], input=json.dumps(data), capture_output=True, text=True, env=env,
                           cwd=str(folder), timeout=30)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))
        return time.monotonic() - started

    def sent(self):
        return [r for r in self.api.presence_calls() if r["path"].endswith(presence_id(self.session))]

    def test_each_folder_reports_with_its_own_workspaces_token(self):
        self.workspace("org_Alpha", fake_api.TOKEN)
        self.workspace("org_Beta", OTHER)
        (self.conf / "default-workspace").write_text("org_Beta\n")
        (self.conf / "projects").write_text(f"{self.alpha}\torg_Alpha\n")
        self.run_hook(self.alpha)
        wait_for(self, lambda: self.sent(), 10, "the mapped folder reported nothing")
        self.assertEqual(self.sent()[0]["authorization"], f"Bearer {fake_api.TOKEN}")
        self.session = str(uuid.uuid4())
        self.run_hook(self.beta)
        wait_for(self, lambda: self.sent(), 10, "the default workspace's folder reported nothing")
        self.assertEqual(self.sent()[0]["authorization"], f"Bearer {OTHER}", "a folder used another's token")

    def test_the_token_reaches_python_as_a_path_and_never_as_a_value(self):
        self.workspace("org_Alpha", fake_api.TOKEN)
        (self.conf / "default-workspace").write_text("org_Alpha\n")
        spy = self.tmp / "spy"
        spy.mkdir()
        log = self.tmp / "python.log"
        (spy / "python3").write_text(f'#!/bin/sh\n{{ printf "%s\\n" "$*"; env; }} >> "{log}"\n'
                                     f'exec "{sys.executable}" "$@"\n')
        (spy / "python3").chmod(0o755)
        env = self.env()
        env["PATH"] = f"{spy}{os.pathsep}{env['PATH']}"
        self.run_hook(self.alpha, env=env)
        wait_for(self, lambda: self.sent(), 10, "nothing was reported")
        seen = log.read_text()
        self.assertNotIn(fake_api.TOKEN, seen)
        self.assertIn(f"UNIO_SESSION_TOKEN_FILE={self.conf / 'workspaces' / 'org_Alpha' / 'token'}", seen)
        self.assertIn(f"UNIO_SESSION_API={self.api.base}", seen)

    def test_an_environment_variable_never_moves_the_api(self):
        self.workspace("org_Alpha", fake_api.TOKEN)
        (self.conf / "default-workspace").write_text("org_Alpha\n")
        (self.conf / "api-url").unlink()
        elsewhere = fake_api.FakeApi().start()
        self.addCleanup(elsewhere.stop)
        self.run_hook(self.alpha, env=self.env(UNIO_SESSION_API=elsewhere.base, UNIO_API_URL=elsewhere.base))
        time.sleep(1.5)
        self.assertEqual(elsewhere.requests, [], "the token followed an environment variable")

    def test_without_python_the_hook_does_nothing_and_says_nothing(self):
        self.workspace("org_Alpha", fake_api.TOKEN)
        (self.conf / "default-workspace").write_text("org_Alpha\n")
        bare = self.bare_path()
        for stub in (None, "exit 9009", "exit 1"):  # none; the Store alias; a Python older than 3.9
            with self.subTest(stub):
                for name in ("python3", "python"):
                    (bare / name).unlink(missing_ok=True)
                    if stub:
                        (bare / name).write_text(f"#!/bin/sh\n{stub}\n")
                        (bare / name).chmod(0o755)
                env = self.env()
                env["PATH"] = str(bare)
                self.assertLess(self.run_hook(self.alpha, env=env), 3)
        time.sleep(1)
        self.assertEqual(self.api.requests, [])
        self.assertFalse(self.sessions.exists())

    def test_an_inbox_run_starts_no_python(self):
        self.workspace("org_Alpha", fake_api.TOKEN)
        (self.conf / "default-workspace").write_text("org_Alpha\n")
        self.run_hook(self.alpha, env=self.env(UNIO_INBOX_RUN="1"))
        time.sleep(1)
        self.assertEqual(self.api.requests, [])

    def test_a_folder_whose_workspace_has_no_token_reports_nothing(self):
        self.workspace("org_Beta", OTHER)
        (self.conf / "default-workspace").write_text("org_Beta\n")
        (self.conf / "projects").write_text(f"{self.alpha}\torg_Gamma\n")
        self.run_hook(self.alpha)
        time.sleep(1.5)
        self.assertEqual(self.api.requests, [], "another workspace's token was used")

    def bare_path(self):
        """A folder of the tools the scripts call, and no Python."""
        bare = self.tmp / "bare"
        bare.mkdir(exist_ok=True)
        for name in TOOLS:
            for folder in posix_path().split(os.pathsep):
                if (Path(folder) / name).exists() and not (bare / name).exists():
                    (bare / name).symlink_to(Path(folder) / name)
        return bare


@unittest.skipIf(WINDOWS, ON_WINDOWS)
class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-doctor-test-"))
        self.addCleanup(remove_tree, self.tmp)

    def doctor(self, path):
        env = base_env(self.tmp, PATH=path)
        return subprocess.run(["sh", str(SCRIPTS / "doctor.sh"), "claude-code"], capture_output=True, text=True,
                              env=env, stdin=subprocess.DEVNULL, timeout=60, encoding="utf-8").stdout

    def test_it_says_whether_the_session_status_is_on(self):
        report = self.doctor(posix_path())
        self.assertIn("ok core sessions: as sessões do Claude Code neste computador aparecem no Unio", report)

    def test_without_python_it_says_how_to_turn_it_on(self):
        bare = Launcher.bare_path(self)
        report = self.doctor(str(bare))
        line = next((x for x in report.splitlines() if "core sessions:" in x), "")
        self.assertTrue(line.startswith("info core sessions: as sessões do Claude Code neste computador não "
                                        "aparecem no Unio, porque falta o Python 3.9 ou mais novo; "), report)
        self.assertIn("xcode-select --install" if sys.platform == "darwin" else "instale o pacote python3", line)


class Hooks(unittest.TestCase):
    """hooks.json registers the launcher for every event it needs, in shell form."""

    def test_every_event_runs_the_launcher_and_only_the_end_and_start_hold_the_session(self):
        hooks = json.loads((SCRIPTS.parent / "hooks" / "hooks.json").read_text())["hooks"]
        command = 'sh "${CLAUDE_PLUGIN_ROOT}/scripts/session-status.sh"'
        found = {}
        for event, groups in hooks.items():
            for group in groups:
                for h in group["hooks"]:
                    if h["command"] == command:
                        found[event] = (group.get("matcher"), bool(h.get("async")), h["timeout"])
        self.assertEqual(found, {
            "SessionStart": (None, False, 5),
            "UserPromptSubmit": (None, True, 10),
            "PostToolUse": ("mcp__plugin_unio_unio__.*|mcp__unio__.*", True, 10),
            "Stop": (None, True, 10),
            "SessionEnd": (None, False, 5),
        })


if __name__ == "__main__":
    unittest.main()
