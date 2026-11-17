"""A reply that runs out of time: put down to a slow cause when one is
credited, still "too large" otherwise, and never carrying the owner's
figures or program names into the thread or the settlement."""
import json
import os
from pathlib import Path
from unittest import mock

from inbox_support import FakeUnio, HomeCase, inbox, write_fake_claude
from test_inbox_e2e import TASK_ID, mention
from test_inbox_vitals_wiring import StubVitals

GB = 2**30
# Held for 120 s of the run: credited whatever the run's length here.
CREDITED = {"wallS": 2.0, "slow": ["slow_memory"], "causeS": {"slow_memory": 120}, "stall": {}, "psi": {}}
UNCREDITED = {"wallS": 2.0, "slow": ["slow_memory"], "causeS": {"slow_memory": 10}, "stall": {}, "psi": {}}
PROGRAM = "evilprog"


def heavy(stub):
    """A memory episode as the owner sees it, heaviest program included."""
    view = {"mem": {"avail": 1.2 * GB, "total": 31.4 * GB, "swapTotal": 8 * GB, "swapFree": 0.2 * GB}}
    stub.on_change("slow_memory", inbox.owner_message(["slow_memory"], view, [(PROGRAM, 6.9 * GB)]))


class Timeout(HomeCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        self.fake.items = [mention()]
        bin_dir = self.tmp / "bin"
        write_fake_claude(bin_dir)
        os.environ.update(UNIO_API_URL=self.fake.url, FAKE_CLAUDE_LOG=str(self.tmp / "claude.jsonl"),
                          PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                          FAKE_CLAUDE_MODE="hang", UNIO_INBOX_TIMEOUT="2")
        os.environ.pop("UNIO_INBOX_CLAUDE", None)
        self.profile("org_test", FakeUnio.RUNNER, {"ABC": []})
        self.state = Path(inbox.profile_dirs("org_test")[1])

    def once(self, summary, on_start=heavy):
        runner = inbox.Runner(inbox.Config(os.environ, "org_test"), vitals=StubVitals(summary, on_start))
        with mock.patch.object(inbox, "desk_memory", return_value=None):
            self.assertEqual(runner.locked_poll(once=True), 1)

    def reply(self):
        replies = self.fake.seen("POST", f"/v1/tasks/{TASK_ID}/comments")
        self.assertEqual(len(replies), 1)
        return replies[0]["body"]["content"]

    def done(self):
        return self.fake.seen("POST", "/v1/agents/inbox/complete")[-1]["body"]

    def item(self):
        lines = (self.state / "log.jsonl").read_text(encoding="utf-8").splitlines()
        item, = [e for e in map(json.loads, lines) if e["event"] == "item"]
        return item

    def test_a_credited_cause_names_itself_in_the_reply_and_the_note(self):
        self.once(CREDITED)
        self.assertEqual(self.reply(), "**Não consegui responder a tempo.** Falta memória para o agente trabalhar no "
                                       "ritmo normal. Me chame de novo em alguns minutos.")
        self.assertEqual(self.done()["note"], "timed out (slow_memory)")
        item = self.item()
        self.assertEqual((item["cause"], item["slow"], item["note"]),
                         ("slow_memory", ["slow_memory"], "timed out (slow_memory)"))

    def test_the_reply_follows_the_writing_language(self):
        self.fake.language = "en"
        self.once(CREDITED)
        self.assertEqual(self.reply(), "**I could not answer in time.** The agent is short of memory to work at its "
                                       "normal pace. Mention me again in a few minutes.")

    def test_without_a_credited_cause_it_stays_too_large(self):
        self.once(UNCREDITED)
        self.assertIn("grande demais", self.reply())
        self.assertEqual(self.done()["note"], inbox.TOO_LARGE_NOTE)
        self.assertNotIn("cause", self.item())

    def test_without_vitals_it_stays_too_large(self):
        runner = inbox.Runner(inbox.Config(os.environ, "org_test"))
        self.assertIsNone(runner.vitals)
        with mock.patch.object(inbox, "desk_memory", return_value=None):
            runner.locked_poll(once=True)
        self.assertEqual(self.done()["note"], inbox.TOO_LARGE_NOTE)

    def test_the_turn_cap_is_no_slowness(self):
        os.environ["FAKE_CLAUDE_MODE"] = "too_large"
        self.once(CREDITED)
        self.assertIn("grande demais", self.reply())
        self.assertEqual(self.done()["note"], inbox.TOO_LARGE_NOTE)

    def test_no_figure_or_program_name_leaves_through_the_thread_or_the_settlement(self):
        self.once(CREDITED)
        presence = [r for r in self.fake.seen() if r["path"].startswith("/v1/agents/presence/")]
        # The fixture did reach the owner's channel, so its absence elsewhere means something.
        self.assertTrue(any(PROGRAM in (r["body"].get("message") or "") for r in presence))
        public = [r for r in self.fake.seen() if not r["path"].startswith("/v1/agents/presence/")]
        self.assertTrue(any(r["path"] == "/v1/agents/inbox/complete" for r in public))
        for r in public:
            text = json.dumps(r["body"], ensure_ascii=False) if r["body"] is not None else ""
            for marker in (PROGRAM, "GB", "Memória", "swap"):
                self.assertNotIn(marker, text, f"{r['method']} {r['path']}")


class Copy(HomeCase):
    def test_every_cause_has_a_sentence_in_each_language(self):
        for language in inbox.LANGUAGE_NAMES:
            with self.subTest(language=language):
                self.assertEqual(set(inbox.SLOW_SENTENCES[language]), set(inbox.SLOW_CODES))
                self.assertIn("{cause}", inbox.SLOW_TIMEOUT_REPLY[language])
                for text in [*inbox.SLOW_SENTENCES[language].values(), inbox.SLOW_TIMEOUT_REPLY[language]]:
                    self.assertNotIn("—", text)
                    self.assertNotRegex(text, r"(?i)claude|anthropic")
