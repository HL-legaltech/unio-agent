"""The runner's token block in health.json: the expiry it reads from
GET /agents/me, a refusal kept as one line and a count across restarts, the
backed-off polls and the held presence."""
import json
import os
import signal
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from inbox_support import FAR_EXPIRY, FakeUnio, HomeCase, inbox

START = datetime(2026, 11, 21, 18, 0, 0, tzinfo=timezone.utc)


class Token(HomeCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        os.environ["UNIO_API_URL"] = self.fake.url
        os.environ["UNIO_INBOX_CLAUDE"] = "claude-not-run"
        self.config = self.profile("org_test", FakeUnio.RUNNER, {"ABC": []})
        self.state = Path(inbox.profile_dirs("org_test")[1])
        for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGBREAK"):
            if hasattr(signal, name):
                self.addCleanup(signal.signal, getattr(signal, name), signal.getsignal(getattr(signal, name)))
        patcher = mock.patch.object(inbox, "desk_memory", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def once(self):
        """One --once run: a new process's Runner."""
        return inbox.main(["--workspace", "org_test", "--once"])

    def runner(self):
        return inbox.Runner(inbox.Config(dict(os.environ), "org_test"))

    def poll(self, runner, at):
        with mock.patch.object(inbox, "utcnow", return_value=at):
            return runner.poll()

    def health(self):
        return json.loads((self.state / "health.json").read_text(encoding="utf-8"))

    def block(self):
        return self.health().get("token")

    def events(self, name):
        try:
            lines = (self.state / "log.jsonl").read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [e for e in map(json.loads, lines) if e.get("event") == name]

    def me_calls(self):
        return len(self.fake.seen("GET", "/v1/agents/me"))

    def write_block(self, block):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "health.json").write_text(json.dumps({"token": block}), encoding="utf-8")

    def test_the_expiry_is_read_at_start_and_kept(self):
        self.assertEqual(self.once(), 0)
        block = self.block()
        self.assertEqual(block["expiresAt"], FAR_EXPIRY)
        self.assertEqual(block["state"], "ok")
        self.assertEqual(block["agentName"], "Agent Marker")
        self.assertIsNotNone(inbox.parse_iso(block["checkedAt"]))
        self.assertEqual(self.me_calls(), 1)

    def test_a_fresh_check_is_not_repeated_within_a_process(self):
        runner = self.runner()
        self.poll(runner, START)
        self.poll(runner, START + timedelta(minutes=1))
        self.assertEqual(self.me_calls(), 1)

    def test_six_hours_later_it_is(self):
        runner = self.runner()
        self.poll(runner, START)
        self.poll(runner, START + timedelta(hours=6))
        self.assertEqual(self.me_calls(), 2)

    def test_a_failed_check_waits_15_minutes(self):
        self.fake.me_status = 503
        runner = self.runner()
        self.poll(runner, START)
        self.poll(runner, START + timedelta(minutes=1))
        self.assertEqual(self.me_calls(), 1)
        self.poll(runner, START + timedelta(minutes=16))
        self.assertEqual(self.me_calls(), 2)

    def test_the_last_week_is_logged_once(self):
        self.fake.expires_at = inbox.iso(START + timedelta(days=3))
        runner = self.runner()
        self.poll(runner, START)
        self.poll(runner, START + timedelta(hours=6))
        self.assertEqual(len(self.events("token_expiring")), 1)
        self.assertEqual(self.events("token_expiring")[0]["days"], 3)
        self.assertEqual(self.block()["state"], "expiring")

    @unittest.skipUnless(hasattr(time, "tzset"), "TZ is read only on POSIX")
    def test_days_left_counts_local_midnights(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "America/Sao_Paulo"
        time.tzset()

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

        self.addCleanup(restore)
        # 07:00 and, two days later, 08:00 in São Paulo (UTC-3): 49 hours, which the server's ceil reads as 3.
        now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(inbox.days_left(datetime(2026, 11, 22, 11, 0, tzinfo=timezone.utc), now), 2)

    def test_a_refused_token_is_one_line_and_a_count(self):
        self.fake.refused.add(FakeUnio.RUNNER)
        runner = self.runner()
        for minutes in (0, 1, 5):
            self.assertIsNone(self.poll(runner, START + timedelta(minutes=minutes)))
        self.assertEqual(len(self.events("token_refused")), 1)
        self.assertEqual(self.events("poll_error"), [])
        block = self.block()
        self.assertEqual((block["state"], block["refusedCount"], block["refusedSince"]),
                         ("refused", 3, inbox.iso(START)))
        self.assertEqual(block["lastRefusedAt"], inbox.iso(START + timedelta(minutes=5)))
        self.assertEqual(self.events("token_refused")[0]["code"], "invalid_agent_token")
        # The health file still says what the last poll met.
        self.assertIn("401", self.health()["lastError"])

    def test_a_restart_keeps_the_refusal(self):
        self.fake.refused.add(FakeUnio.RUNNER)
        self.assertEqual(self.once(), 1)
        first = self.block()["refusedSince"]
        runner = self.runner()
        self.assertEqual(runner.locked_poll(once=True), None)
        self.assertEqual(len(self.events("token_refused")), 1)
        block = self.block()
        self.assertEqual((block["refusedSince"], block["refusedCount"]), (first, 2))
        self.assertEqual(runner.next_wait(), 120)

    def test_a_refusal_with_no_known_expiry_says_unknown(self):
        self.fake.refused.add(FakeUnio.RUNNER)
        self.poll(self.runner(), START)
        self.assertEqual(self.block()["cause"], "unknown")

    def test_a_refusal_after_the_expiry_says_expired(self):
        self.fake.refused.add(FakeUnio.RUNNER)
        self.write_block({"state": "ok", "expiresAt": inbox.iso(START - timedelta(hours=1))})
        self.poll(self.runner(), START)
        self.assertEqual(self.block()["cause"], "expired")
        self.assertEqual(self.events("token_refused")[0]["cause"], "expired")

    def test_a_refusal_before_the_expiry_says_refused(self):
        self.fake.refused.add(FakeUnio.RUNNER)
        self.write_block({"state": "ok", "expiresAt": inbox.iso(START + timedelta(days=30))})
        self.poll(self.runner(), START)
        self.assertEqual(self.block()["cause"], "refused")

    def test_a_new_token_after_a_restart_is_accepted(self):
        self.fake.refused.add(FakeUnio.RUNNER)
        self.assertEqual(self.once(), 1)
        (self.config / "runner-token").write_text(FakeUnio.RUNNER2 + "\n", encoding="utf-8")
        before = self.me_calls()
        self.assertEqual(self.once(), 0)
        accepted = self.events("token_accepted")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["refused"], 1)
        block = self.block()
        for key in ("refusedSince", "lastRefusedAt", "refusedCount", "cause"):
            self.assertNotIn(key, block)
        self.assertEqual(block["state"], "ok")
        self.assertEqual(block["expiresAt"], FAR_EXPIRY)
        self.assertEqual(self.me_calls(), before + 1)

    def test_teams_refused_mid_item_counts_nothing(self):
        # Only the inbox decides a refusal: /agents/me answers 401 while it answers 200.
        self.write_block({"state": "ok", "expiresAt": FAR_EXPIRY})
        self.fake.me_status = 401
        runner = self.runner()
        with mock.patch.object(runner.presence, "hold") as hold:
            self.assertEqual(self.poll(runner, START), 0)
            self.assertEqual(self.poll(runner, START + timedelta(minutes=16)), 0)
        self.assertEqual(self.me_calls(), 2)
        self.assertNotIn(mock.call(True), hold.call_args_list)
        self.assertEqual(self.events("token_refused"), [])
        block = self.block()
        self.assertNotIn("refusedCount", block)
        self.assertNotIn("refusedSince", block)
        self.assertEqual(block["state"], "ok")
        self.assertEqual(runner.next_wait(), runner.cfg.interval)

    def test_refused_polls_back_off(self):
        runner = self.runner()
        self.assertEqual(runner.cfg.interval, 30)
        waits = []
        for count in range(1, 6):
            runner.token = {"state": "refused", "refusedCount": count}
            waits.append(runner.next_wait())
        self.assertEqual(waits, [60, 120, 240, 300, 300])
        runner.token = {"state": "missing"}
        self.assertEqual(runner.next_wait(), runner.cfg.interval)

    def test_presence_is_held_while_refused(self):
        self.fake.refused.add(FakeUnio.RUNNER)
        self.once()
        self.once()
        presence = [r for r in self.fake.seen("PUT") if r["path"].startswith("/v1/agents/presence/")]
        self.assertEqual(presence, [])
        self.fake.refused.clear()
        self.assertEqual(self.once(), 0)
        presence = [r for r in self.fake.seen("PUT") if r["path"].startswith("/v1/agents/presence/")]
        self.assertEqual([r["body"]["state"] for r in presence], ["idle"])

    def test_a_release_sends_the_accepted_poll_state(self):
        # A serving process: the refused poll left a problem body behind the hold.
        self.fake.refused.add(FakeUnio.RUNNER)
        runner = self.runner()
        self.poll(runner, START)
        self.assertEqual(runner.presence.want["state"], "problem")
        self.fake.refused.clear()
        released = []
        hold = runner.presence.hold

        def record(on):
            hold(on)
            if not on:
                released.append(runner.presence._body()["state"])

        with mock.patch.object(runner.presence, "hold", side_effect=record):
            self.poll(runner, START + timedelta(minutes=1))
        self.assertEqual(released, ["idle"])

    def test_the_held_presence_sends_nothing(self):
        cfg = inbox.Config(dict(os.environ), "org_test")
        p = inbox.Presence(cfg)
        p.id = "00000000-0000-4000-8000-000000000001"
        p.hold(True)
        p.set("problem", reason="unio_unavailable")
        p._send()
        self.assertEqual(self.fake.seen("PUT"), [])
        p.hold(False)
        self.assertIsNone(p.sent)
        p._send()
        self.assertEqual(len(self.fake.seen("PUT")), 1)

    def test_a_missing_token_is_one_line(self):
        (self.config / "runner-token").unlink()
        runner = self.runner()
        for minutes in (0, 1, 2):
            self.poll(runner, START + timedelta(minutes=minutes))
        self.assertEqual(len(self.events("token_missing")), 1)
        self.assertEqual(self.events("poll_error"), [])
        self.assertEqual(self.block()["state"], "missing")
        self.assertEqual(self.fake.seen(), [])

    def test_the_block_never_holds_the_token(self):
        self.once()
        self.fake.refused.add(FakeUnio.RUNNER)
        self.once()
        for name in ("health.json", "log.jsonl"):
            text = (self.state / name).read_text(encoding="utf-8")
            self.assertNotIn(FakeUnio.RUNNER, text, name)
            self.assertNotIn(FakeUnio.RUNNER[:12], text, name)


if __name__ == "__main__":
    unittest.main()
