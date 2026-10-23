"""unio-runner and unio-inbox together: a profile that unio-runner setup
made, as a person runs it, passes unio-inbox --check and answers a mention
in one --once run, with nothing but the profile to say where Unio is."""
import json
import os
import signal
import subprocess
import sys
from unittest import mock

import inbox_support
import test_inbox_e2e
from inbox_support import FakeUnio, HomeCase, inbox, write_fake_claude
from runner_support import UNIO_RUNNER, fake_token


class SetUpThenAnswer(HomeCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        # unio-runner takes only a token in the real shape.
        self.fake.RUNNER = fake_token("SetUpThenAnswerFakeTokenNotASecret")
        self.fake.items = [test_inbox_e2e.mention()]
        bin_dir = self.tmp / "bin"
        write_fake_claude(bin_dir)
        self.claude_log = self.tmp / "claude.jsonl"
        os.environ.update(FAKE_CLAUDE_LOG=str(self.claude_log), NO_PROXY="127.0.0.1,localhost",
                          PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        # Where Unio is must come from the profile setup writes, never from this shell.
        for key in list(os.environ):
            if (key.startswith("UNIO_") and key != "UNIO_RUNNER_AUTOUPDATE") or key == "CLAUDECODE":
                del os.environ[key]
        for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGBREAK"):
            if hasattr(signal, name):
                self.addCleanup(signal.signal, getattr(signal, name), signal.getsignal(getattr(signal, name)))

    def test_setup_check_and_answer(self):
        # The runner answering below is the one unio-runner runs.
        self.assertEqual(inbox_support.RUNNER, UNIO_RUNNER.with_name("unio-inbox"))
        repos = self.tmp / "repos.json"
        repos.write_text(json.dumps({"ABC": []}), encoding="utf-8")
        done = subprocess.run(
            [sys.executable, str(UNIO_RUNNER), "setup", "--workspace", "org_test", "--api", self.fake.url,
             "--token-stdin", "--repos-json", str(repos), "--no-service"],
            input=self.fake.RUNNER + "\n", capture_output=True, encoding="utf-8", errors="replace",
            env=dict(os.environ), timeout=180)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, out)
        self.assertEqual(out.splitlines()[-1], "Perfil do serviço de respostas pronto para Workspace Marker.")
        self.assertNotIn(self.fake.RUNNER, out)

        check = self.run_inbox("--workspace", "org_test", "--check")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertTrue(json.loads(check.stdout)["ok"])

        with mock.patch.object(inbox, "desk_memory", return_value=None):
            self.assertEqual(inbox.main(["--workspace", "org_test", "--once"]), 0)
        claim = self.fake.seen("POST", "/v1/agents/inbox/claim")[0]
        self.assertEqual(claim["token"], self.fake.RUNNER)
        complete = self.fake.seen("POST", "/v1/agents/inbox/complete")[0]["body"]
        self.assertEqual((complete["key"], complete["outcome"]), ("comment:c-0001", "replied"))
        self.assertEqual(self.fake.items, [])
        session, = [json.loads(line) for line in self.claude_log.read_text(encoding="utf-8").splitlines()
                    if line.startswith("{\"argv\"")]
        self.assertIn("Writing language: Brazilian Portuguese.", session["prompt"])
        self.assertIn("- **Product (ABC):** No repository.", session["protocol"])
