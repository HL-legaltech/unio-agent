"""One --workspace --once run, end to end: the runner as main() starts it,
a fake Unio API on 127.0.0.1, and a fake claude found on PATH that answers
through the MCP server it is given."""
import contextlib
import json
import os
import shutil
import signal
from pathlib import Path
from unittest import mock

from inbox_support import FakeUnio, HomeCase, inbox, non_utf8_locale, write_fake_claude

TASK_ID = "11111111-1111-4111-8111-111111111111"


def mention():
    return {
        "key": "comment:c-0001", "kind": "mention", "subject": "task", "attempts": 0,
        "createdAt": "2026-10-23T12:00:00.000Z",
        "task": {"id": TASK_ID, "key": "ABC-1", "url": "https://unio.so/app/tasks/ABC-1/titulo?ws=org_test",
                 "title": "Título da tarefa", "team": {"id": 1, "name": "Product", "identifier": "ABC"},
                 "status": {"name": "Todo", "category": "unstarted"}},
        "comment": {"id": "c-0001", "rootId": None, "content": "@Agent pode explicar?",
                    "createdAt": "2026-10-23T12:00:00.000Z",
                    "author": {"kind": "user", "name": "Person Marker", "clerkId": "user_person",
                               "canUpdateTasks": True, "canCreateTasks": True}},
    }


class Once(HomeCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        self.fake.items = [mention()]
        bin_dir = self.tmp / "bin"
        write_fake_claude(bin_dir)
        self.claude_log = self.tmp / "claude.jsonl"
        os.environ.update(UNIO_API_URL=self.fake.url, FAKE_CLAUDE_LOG=str(self.claude_log),
                          PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        os.environ.pop("UNIO_INBOX_CLAUDE", None)
        self.config = self.profile("org_test", FakeUnio.RUNNER, {"ABC": []})
        self.state = Path(inbox.profile_dirs("org_test")[1])
        # main() takes the process's signals; give them back afterwards.
        for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGBREAK"):
            if hasattr(signal, name):
                self.addCleanup(signal.signal, getattr(signal, name), signal.getsignal(getattr(signal, name)))

    def once(self):
        # The memory guard reads this machine's /proc, which a test must not depend on.
        with mock.patch.object(inbox, "desk_memory", return_value=None):
            return inbox.main(["--workspace", "org_test", "--once"])

    @contextlib.contextmanager
    def small_machine(self):
        """Linux with 4 GiB, 3 of them available, and no swap, as many small servers are."""
        meminfo = self.tmp / "meminfo"
        meminfo.write_text("MemTotal: 4194304 kB\nMemAvailable: 3145728 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
                           encoding="utf-8")
        with mock.patch.object(inbox.sys, "platform", "linux"), mock.patch.object(inbox, "MEMINFO", str(meminfo)), \
                mock.patch.object(inbox, "DESK_FILE", self.tmp / "no-desk.json"):
            yield

    def test_a_small_machine_without_swap(self):
        # The workstation floors would hold every answer here.
        with self.small_machine():
            self.assertEqual(inbox.main(["--workspace", "org_test", "--once"]), 0)
        self.assertEqual(len(self.fake.seen("POST", "/v1/agents/inbox/complete")), 1)
        self.assertEqual(self.fake.seen("POST", "/v1/agents/inbox/release"), [])

    def sessions(self):
        return [json.loads(line) for line in self.claude_log.read_text(encoding="utf-8").splitlines()]

    def log(self):
        return [json.loads(line) for line in (self.state / "log.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_claims_reacts_answers_and_completes(self):
        self.assertEqual(self.once(), 0)
        calls = [(r["method"], r["path"]) for r in self.fake.seen() if not r["path"].startswith("/v1/agents/presence/")]
        order = [("GET", "/v1/agents/inbox"), ("POST", "/v1/agents/inbox/claim"), ("POST", "/v1/mcp"),
                 ("POST", "/v1/agents/inbox/complete"), ("DELETE", "/v1/agents/reactions")]
        self.assertEqual([c for c in calls if c in order], order)
        self.assertIn(("PUT", "/v1/agents/reactions"), calls)
        self.assertLess(calls.index(("PUT", "/v1/agents/reactions")), calls.index(("POST", "/v1/mcp")))

        claim = self.fake.seen("POST", "/v1/agents/inbox/claim")[0]
        self.assertEqual(claim["body"]["key"], "comment:c-0001")
        self.assertIn("presenceId", claim["body"])
        self.assertEqual(self.fake.seen("PUT", "/v1/agents/reactions")[0]["body"],
                         {"reactableType": "comment", "reactableId": "c-0001", "emoji": "eyes"})

        answer = self.fake.seen("POST", "/v1/mcp")[0]
        self.assertEqual(answer["headers"]["X-Unio-Inbox-Item"], "comment:c-0001")
        self.assertEqual(answer["headers"]["X-Unio-Inbox-Claim"], "claim-0001-fake")
        self.assertEqual(answer["body"]["params"]["arguments"]["content"], "Resposta de teste.")

        complete = self.fake.seen("POST", "/v1/agents/inbox/complete")[0]["body"]
        self.assertEqual((complete["key"], complete["claimId"], complete["outcome"]),
                         ("comment:c-0001", "claim-0001-fake", "replied"))
        self.assertEqual(self.fake.items, [])

        presence = self.fake.seen("PUT")
        clients = {r["body"].get("client") for r in presence if r["path"].startswith("/v1/agents/presence/")}
        self.assertEqual(clients, {inbox.presence_client()})

        session, = self.sessions()
        self.assertIn("Writing language: Brazilian Portuguese.", session["prompt"])
        self.assertIn("## The workspace", session["protocol"])
        self.assertIn("- **Product (ABC):** No repository.", session["protocol"])
        self.assertNotIn(inbox.WORKSPACE_MARKER, session["protocol"])
        self.assertEqual(session["headers"], ["Authorization", "X-Unio-Inbox-Claim", "X-Unio-Inbox-Item"])
        self.assertIn("--settings", session["argv"])

        item, = [e for e in self.log() if e["event"] == "item"]
        self.assertEqual((item["result"], item["outcome"]), ("completed", "replied"))
        health = json.loads((self.state / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["handled24h"], 1)
        self.assertNotIn("current", health)
        # What Claude Code kept for the session's folder went with the run.
        self.assertEqual(list((Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects").iterdir()), [])
        # The single-machine folders are untouched.
        self.assertFalse((self.home / ".local" / "state" / "unio" / "inbox").exists())

        self.assertEqual(self.once(), 0)
        self.assertEqual(len(self.sessions()), 1, "a completed item is not answered again")

    def test_runner_reply_follows_the_writing_language(self):
        os.environ["FAKE_CLAUDE_MODE"] = "too_large"
        for language, words in (("pt-BR", "grande demais"), ("en", "too large")):
            with self.subTest(language=language):
                self.fake.language = language
                self.fake.items = [mention()]
                before = len(self.fake.seen("POST", f"/v1/tasks/{TASK_ID}/comments"))
                self.assertEqual(self.once(), 0)
                replies = self.fake.seen("POST", f"/v1/tasks/{TASK_ID}/comments")[before:]
                self.assertEqual(len(replies), 1)
                self.assertEqual(replies[0]["body"]["parentId"], "c-0001")
                self.assertIn(words, replies[0]["body"]["content"])
                done = self.fake.seen("POST", "/v1/agents/inbox/complete")[-1]["body"]
                self.assertEqual(done["note"], inbox.TOO_LARGE_NOTE)


class OnceWithoutProfile(Once):
    """The single-machine setup a runner before profiles used: the files in
    ~/.config/unio, its own responder protocol used as it is, the state in
    ~/.local/state/unio/inbox. Only the writing language is new there."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(self.config.parent)
        self.legacy = self.home / ".config" / "unio"
        self.legacy.mkdir(parents=True, exist_ok=True)
        (self.legacy / "token").write_text(FakeUnio.SESSION + "\n", encoding="utf-8")
        (self.legacy / "repos.json").write_text('{"ABC": []}', encoding="utf-8")
        self.responder = self.legacy / "responder.md"
        self.responder.write_text("# Our own protocol\n\n## The workspace\n\nOur teams.\n", encoding="utf-8")
        self.state = self.home / ".local" / "state" / "unio" / "inbox"

    def once(self):
        with mock.patch.object(inbox, "desk_memory", return_value=None):
            return inbox.main(["--once"])

    def test_claims_reacts_answers_and_completes(self):
        self.assertEqual(self.once(), 0)
        session, = self.sessions()
        argv = session["argv"]
        self.assertEqual(argv[argv.index("--append-system-prompt-file") + 1], str(self.responder))
        self.assertEqual(session["protocol"], self.responder.read_text(encoding="utf-8"))
        self.assertIn("Writing language: Brazilian Portuguese.", session["prompt"])
        self.assertEqual(self.fake.seen("POST", "/v1/agents/inbox/complete")[0]["body"]["outcome"], "replied")
        self.assertTrue((self.state / "health.json").is_file())
        self.assertTrue((self.legacy / "runner-id").is_file())
        self.assertFalse((self.home / ".config" / "unio" / "runners").exists())
        self.assertFalse((self.home / ".local" / "state" / "unio" / "runners").exists())

    def test_a_small_machine_without_swap(self):
        # The floors measured on the workstation this setup serves still hold.
        with self.small_machine():
            self.assertEqual(inbox.main(["--once"]), 0)
        self.assertEqual(self.fake.seen("POST", "/v1/agents/inbox/claim"), [])
        health = json.loads((self.state / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["backoffReason"], "memory_pressure")

    def test_the_local_responder_is_still_required(self):
        self.responder.unlink()
        self.assertEqual(self.once(), 0)
        self.assertEqual(self.fake.seen("POST", "/v1/agents/inbox/claim"), [])
        health = json.loads((self.state / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["backoffReason"], "responder_missing")


class DryRun(HomeCase):
    def test_into_a_pipe_it_is_utf8(self):
        item = mention()
        item["comment"]["content"] = "Você pode explicar a seção?"
        self.profile("org_test", FakeUnio.RUNNER)
        with FakeUnio() as fake:
            fake.items = [item]
            p = self.run_inbox("--workspace", "org_test", "--dry-run",
                               env=non_utf8_locale(dict(os.environ, UNIO_API_URL=fake.url)))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Person Marker: Você pode explicar a seção?", p.stdout)
