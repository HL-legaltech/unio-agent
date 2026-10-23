"""--check: what unio-runner asks before it installs a service."""
import json
import os
import socket
import sys
from pathlib import Path

from inbox_support import FakeUnio, HomeCase, inbox


class Check(HomeCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        os.environ["UNIO_API_URL"] = self.fake.url
        # Any file stands in for Claude Code: --check only looks for it.
        os.environ["UNIO_INBOX_CLAUDE"] = sys.executable

    def check(self, token=None, workspace="org_test"):
        if token is not None:
            self.profile(workspace, token)
        p = self.run_inbox("--workspace", workspace, "--check")
        result = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0 if result["ok"] else 1, p.stderr)
        self.assertEqual(len(result["problems"]), len(result["codes"]))
        return result

    def test_ok(self):
        result = self.check(FakeUnio.RUNNER)
        self.assertEqual(result, {"ok": True, "workspace": {"clerkId": "org_test", "name": "Workspace Marker"},
                                  "agent": {"name": "Agent Marker"}, "language": "pt-BR", "problems": [],
                                  "codes": []})
        self.assertEqual([(r["method"], r["path"]) for r in self.fake.seen()], [("GET", "/v1/agents/me")])

    def test_writes_nothing(self):
        self.check(FakeUnio.RUNNER)
        config, state = (Path(p) for p in inbox.profile_dirs("org_test"))
        self.assertFalse(state.exists())
        self.assertFalse((config / "runner-id").exists())

    def test_english_workspace(self):
        self.fake.language = "en"
        self.assertEqual(self.check(FakeUnio.RUNNER)["language"], "en")

    def test_no_token(self):
        result = self.check()
        self.assertEqual(result["codes"], ["no_token"])
        self.assertEqual(self.fake.seen(), [])

    def test_a_person_token_is_not_a_runner_token(self):
        self.assertEqual(self.check(FakeUnio.SESSION)["codes"], ["not_runner_token"])

    def test_wrong_workspace(self):
        result = self.check(FakeUnio.OTHER)
        self.assertEqual(result["codes"], ["wrong_workspace"])
        self.assertIn("org_other", result["problems"][0])

    def test_refused(self):
        self.assertEqual(self.check("unio_rt_revoked")["codes"], ["token_refused"])

    def test_agents_off(self):
        self.assertEqual(self.check(FakeUnio.OFF)["codes"], ["agents_off"])

    def test_claude_missing(self):
        os.environ["UNIO_INBOX_CLAUDE"] = str(self.tmp / "no-claude")
        self.assertEqual(self.check(FakeUnio.RUNNER)["codes"], ["claude_missing"])

    def test_unreachable(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        os.environ["UNIO_API_URL"] = f"http://127.0.0.1:{port}/v1"
        self.assertEqual(self.check(FakeUnio.RUNNER)["codes"], ["unio_unreachable"])

    def test_env_file_names_the_api(self):
        config = self.profile("org_test", FakeUnio.RUNNER)
        (config / "env").write_text(f"UNIO_API_URL={self.fake.url}\n", encoding="utf-8")
        del os.environ["UNIO_API_URL"]
        self.assertTrue(self.check()["ok"])
