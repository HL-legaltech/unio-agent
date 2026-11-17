"""The presence row with a slow cause laid over it: which bodies carry the
reason and the owner's detail, when a change goes out, and a presence that
keeps sending whatever a timing hook or a write throws."""
import time
import unittest
from unittest import mock

from inbox_support import FakeUnio, HomeCase, inbox


class Cfg:
    def __init__(self, api="http://127.0.0.1:9/v1"):
        self.api = api
        self.interval = 30
        self.token_file = "runner-token"

    def runner_id(self):
        return "00000000-0000-4000-8000-000000000001"

    def token(self):
        return FakeUnio.RUNNER


class Bodies(unittest.TestCase):
    def setUp(self):
        self.p = inbox.Presence(Cfg())

    def body(self):
        with self.p.lock:
            return self.p._body()

    def test_working_and_idle_carry_the_reason_and_detail(self):
        self.p.refresh("slow_memory", "Memória: 1,2 de 31,4 GB livres")
        for state in ("working", "idle"):
            with self.subTest(state=state):
                self.p.set(state, itemKey="comment:c-1" if state == "working" else None)
                body = self.body()
                self.assertEqual((body["state"], body["reason"], body["message"]),
                                 (state, "slow_memory", "Memória: 1,2 de 31,4 GB livres"))

    def test_problem_keeps_its_own_reason_and_ended_carries_none(self):
        self.p.refresh("slow_disk", "Disco em espera 41% do tempo")
        self.p.set("problem", reason="memory_pressure", message="the workstation is low on memory")
        self.assertEqual((self.body()["reason"], self.body()["message"]),
                         ("memory_pressure", "the workstation is low on memory"))
        self.p.set("ended")
        self.assertNotIn("reason", self.body())
        self.assertNotIn("message", self.body())

    def test_a_cleared_cause_leaves_both_keys_out(self):
        self.p.set("working", itemKey="comment:c-1")
        self.p.refresh("slow_ai", None)
        self.assertEqual(self.body()["reason"], "slow_ai")
        self.assertNotIn("message", self.body())
        self.p.refresh(None, None)
        self.assertNotIn("reason", self.body())
        self.assertNotIn("message", self.body())

    def test_the_detail_is_cut_at_the_servers_300_characters(self):
        self.p.set("idle")
        self.p.refresh("low_disk_space", "x" * 400)
        self.assertEqual(len(self.body()["message"]), 300)


class Cadence(unittest.TestCase):
    """_wait() against a body sent 1 s ago."""

    def setUp(self):
        self.p = inbox.Presence(Cfg())
        self.p.set("working", itemKey="comment:c-1", step="reading_task")
        self.p.refresh("slow_memory", "Memória: 1,2 de 31,4 GB livres")
        self.sent()

    def sent(self):
        with self.p.lock:
            self.p.sent = dict(self.p._body())
            self.p.sent_at = time.monotonic() - 1

    def wait(self):
        with self.p.lock:
            return self.p._wait()

    def test_an_unchanged_body_waits_for_the_heartbeat(self):
        self.assertAlmostEqual(self.wait(), self.p.heartbeat - 1, delta=0.5)

    def test_a_reason_change_goes_out_at_once(self):
        self.p.refresh("slow_disk", "Disco em espera 41% do tempo")
        self.assertEqual(self.wait(), 0)

    def test_the_end_of_an_episode_goes_out_at_once(self):
        self.p.refresh(None, None)
        self.assertEqual(self.wait(), 0)

    def test_a_message_only_change_does_not_shorten_the_heartbeat(self):
        self.p.refresh("slow_memory", "Memória: 0,9 de 31,4 GB livres")
        self.assertAlmostEqual(self.wait(), self.p.heartbeat - 1, delta=0.5)
        self.assertGreater(self.wait(), inbox.PRESENCE_SPACING)

    def test_a_step_change_still_waits_only_the_spacing(self):
        self.p.set("working", itemKey="comment:c-1", step="writing_reply")
        self.assertAlmostEqual(self.wait(), inbox.PRESENCE_SPACING - 1, delta=0.5)

    def test_a_problem_message_change_keeps_the_spacing(self):
        self.p.set("problem", reason="usage_limit", message="a")
        self.sent()
        self.p.set("problem", reason="usage_limit", message="b")
        self.assertAlmostEqual(self.wait(), inbox.PRESENCE_SPACING - 1, delta=0.5)


class KeepsSending(HomeCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        self.logs = []
        self.p = inbox.Presence(Cfg(self.fake.url), log=lambda event, **f: self.logs.append((event, f)))
        self.addCleanup(self.close)

    def close(self):
        self.p.close(ended=False)

    def puts(self):
        return [r["body"] for r in self.fake.seen("PUT") if r["path"].startswith("/v1/agents/presence/")]

    def until(self, check, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.02)
        return False

    def test_a_timing_hook_that_raises_never_stops_a_write(self):
        health = inbox.ApiHealth()
        health.record = mock.Mock(side_effect=RuntimeError("hook down"))
        with mock.patch.object(inbox, "API_HEALTH", health):
            self.p.start()
            self.p.set("working", itemKey="comment:c-1")
            self.p.refresh("slow_cpu", "Processador em espera 35% do tempo")
            self.assertTrue(self.until(lambda: any(b.get("reason") == "slow_cpu" for b in self.puts())))
        self.assertTrue(health.record.called)
        self.assertEqual(health.hook_error, "RuntimeError: hook down")
        self.assertIn("lastOkAt", self.p.status())

    def test_a_write_that_raises_is_logged_once_and_the_loop_goes_on(self):
        real, calls = self.p._send, []

        def flaky():
            calls.append(1)
            if len(calls) <= 2:
                raise ValueError("bad body")
            real()
        self.p._send = flaky
        with mock.patch.object(inbox, "PRESENCE_SPACING", 0.05):
            self.p.start()
            self.p.set("idle")
            self.assertTrue(self.until(lambda: self.puts()))
        self.assertEqual(self.logs, [("presence_error", {"error": "ValueError: bad body"})])
        self.assertEqual(self.puts()[0]["state"], "idle")


if __name__ == "__main__":
    unittest.main()
