"""The repository snapshots: gh clones what its login may read, and a
public repository is read without gh too, or with gh signed out."""
import json
import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

from inbox_support import HomeCase, inbox

FAKE_GH = r'''
"""A gh that is installed but signed out, as `gh repo clone` then fails."""
import json
import os
import sys

with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
print("To get started with GitHub CLI, please run:  gh auth login", file=sys.stderr)
sys.exit(4)
'''


class Snapshots(HomeCase):
    def setUp(self):
        super().setUp()
        empty = self.tmp / "gitconfig"
        empty.write_text("", encoding="utf-8")
        github = self.tmp / "github"
        # The runner's https://github.com/ addresses lead to local repositories here.
        os.environ.update(GIT_CONFIG_GLOBAL=str(empty), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_COUNT="1",
                          GIT_CONFIG_KEY_0=f"url.{github.as_uri()}/.insteadOf", GIT_CONFIG_VALUE_0=inbox.GITHUB,
                          GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                          GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
        origin = github / "octo-org" / "api.git"
        self.git("init", "--quiet", "--bare", "-b", "main", str(origin))
        work = self.tmp / "work"
        self.git("clone", "--quiet", str(origin), str(work))
        (work / "README.md").write_text("api\n", encoding="utf-8")
        self.git("-C", str(work), "add", "README.md")
        self.git("-C", str(work), "commit", "--quiet", "-m", "first")
        self.git("-C", str(work), "push", "--quiet", "origin", "HEAD:main")
        self.sha = self.git("-C", str(work), "rev-parse", "--short=7", "HEAD")
        self.gh_log = self.tmp / "gh.jsonl"
        os.environ["FAKE_GH_LOG"] = str(self.gh_log)

    def git(self, *args):
        done = subprocess.run(["git", *args], capture_output=True, encoding="utf-8", stdin=subprocess.DEVNULL)
        if done.returncode:
            raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
        return done.stdout.strip()

    def signed_out_gh(self):
        script = self.tmp / "fake_gh.py"
        script.write_text(FAKE_GH.lstrip(), encoding="utf-8")
        if os.name == "nt":
            launcher = self.tmp / "gh.cmd"
            launcher.write_bytes(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n'.encode("utf-8"))
        else:
            launcher = self.tmp / "gh"
            launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n",
                                encoding="utf-8")
            launcher.chmod(0o755)
        return launcher

    def snapshot(self, gh, name="octo-org/api"):
        os.environ["UNIO_INBOX_GH"] = str(gh)
        cfg = inbox.Config(dict(os.environ), "org_test")
        state = inbox.State(cfg)
        state.ensure()
        record = {}
        return inbox.Snapshots(cfg, state).ensure(name, record), record, state

    def logged(self, state):
        return [json.loads(line) for line in state.log_file.read_text(encoding="utf-8").splitlines()] \
            if state.log_file.exists() else []

    def test_a_public_repository_without_gh(self):
        snap, record, state = self.snapshot(self.tmp / "no-gh")
        self.assertIsNotNone(snap, self.logged(state))
        self.assertEqual((snap["repo"], snap["branch"], snap["commit"]), ("octo-org/api", "main", self.sha))
        self.assertEqual(record["sha"], self.sha)
        self.assertTrue((Path(snap["path"]) / "README.md").is_file())
        self.assertEqual(self.logged(state), [])

    def test_a_public_repository_with_gh_signed_out(self):
        gh = self.signed_out_gh()
        snap, _, state = self.snapshot(gh)
        self.assertIsNotNone(snap, self.logged(state))
        self.assertEqual(snap["commit"], self.sha)
        calls = [json.loads(line) for line in self.gh_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls[0][:3], ["repo", "clone", "octo-org/api"])

    def test_neither_way_says_both(self):
        snap, record, state = self.snapshot(self.signed_out_gh(), "octo-org/missing")
        self.assertIsNone(snap)
        unavailable = [e for e in self.logged(state) if e["event"] == "repo_unavailable"]
        self.assertEqual(len(unavailable), 1)
        self.assertIn("gh repo clone: exit 4", unavailable[0]["error"])
        self.assertIn("git clone: exit", unavailable[0]["error"])
        self.assertIn("lastError", record)
        self.assertEqual([p.name for p in state.repos_dir.iterdir()], [])

    def test_the_credential_helper_is_gh_only_when_gh_is_there(self):
        cfg = inbox.Config(dict(os.environ, UNIO_INBOX_GH=str(self.tmp / "no-gh")), "org_test")
        self.assertEqual(inbox.Snapshots(cfg, inbox.State(cfg))._network(),
                         ["-c", "credential.https://github.com.helper="])
        cfg = inbox.Config(dict(os.environ, UNIO_INBOX_GH=str(self.signed_out_gh())), "org_test")
        network = inbox.Snapshots(cfg, inbox.State(cfg))._network()
        self.assertEqual(network[:2], ["-c", "credential.https://github.com.helper="])
        self.assertTrue(network[3].endswith(" auth git-credential"))


if __name__ == "__main__":
    unittest.main()
