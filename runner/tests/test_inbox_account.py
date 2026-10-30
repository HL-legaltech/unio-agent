"""A usage-limit pause and the account behind it: the pause records which
account hit the limit, a sign-in to another account ends it after one probe
that answers, and the same account never spends a probe."""
import json
import os
import unittest
from datetime import timedelta
from unittest import mock

from inbox_support import HomeCase, inbox

SIGNED = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
          "email": "Operator@Example.org", "orgId": "org-1"}


class AccountId(unittest.TestCase):
    def test_names_the_account_without_its_address(self):
        first = inbox.account_id(SIGNED)
        self.assertEqual(len(first), 16)
        self.assertNotIn("example", first)
        self.assertEqual(first, inbox.account_id(dict(SIGNED, email="operator@example.org")))
        self.assertNotEqual(first, inbox.account_id(dict(SIGNED, orgId="org-2")))

    def test_none_only_without_a_sign_in(self):
        self.assertIsNone(inbox.account_id(dict(SIGNED, loggedIn=False)))
        self.assertIsNone(inbox.account_id({"authMethod": "claude.ai"}))
        self.assertIsNone(inbox.account_id(None))

    def test_a_sign_in_that_names_no_account_is_told_apart_by_its_method(self):
        key = inbox.account_id({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"})
        self.assertIsNotNone(key)
        self.assertNotEqual(key, inbox.account_id(SIGNED))


class LimitRecheck(HomeCase):
    def setUp(self):
        super().setUp()
        self.runner = inbox.Runner(inbox.Config(dict(os.environ), "org_test"))
        self.runner.state.ensure()
        self.now = inbox.utcnow()
        self.until = self.now + timedelta(days=5)
        patcher = mock.patch.object(inbox, "desk_memory", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def paused(self, account="old", reason="usage_limit"):
        h = {"backoffUntil": inbox.iso(self.until), "backoffReason": reason, "backoffDetail": None,
             "usageLimitAt": inbox.iso(self.now - timedelta(hours=1)),
             "quota": {"seven_day": {"utilization": 1.0, "at": inbox.iso(self.now)}}}
        if account:
            h["backoffAccount"] = account
        return h

    def recheck(self, h, account, verdict=("ok", None), at=None):
        self.runner.claude_account = mock.Mock(return_value=account)
        self.runner.probe = mock.Mock(return_value=verdict)
        self.runner.recheck_limit(None, h, at or self.now)
        return self.runner.probe

    def log_events(self):
        path = self.runner.state.dir / "log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]

    def test_the_pause_records_the_account_that_hit_the_limit(self):
        self.runner.account = "old"
        h = {}
        self.runner.pause(h, "usage_limit", "You've hit your weekly limit", self.until)
        self.assertEqual(h["backoffAccount"], "old")

    def test_another_account_that_answers_ends_the_pause(self):
        h = self.paused()
        probe = self.recheck(h, "new")
        probe.assert_called_once()
        self.assertIsNone(h["backoffUntil"])
        self.assertIsNone(h["backoffReason"])
        self.assertIsNone(h["backoffAccount"])
        # The old account's readings and the reviews' quiet time go with it.
        self.assertEqual(h["quota"], {})
        self.assertIsNone(h["usageLimitAt"])
        self.assertIn("resumed", self.log_events())

    def test_the_same_account_spends_no_probe(self):
        h = self.paused()
        probe = self.recheck(h, "old")
        probe.assert_not_called()
        self.assertEqual(h["backoffUntil"], inbox.iso(self.until))

    def test_a_pause_that_named_no_account_is_probed_once(self):
        # A pause written by 2.4.2 and older, which recorded no account.
        h = self.paused(account=None)
        limited = ("usage_limit", "You've hit your weekly limit · resets Nov 11, 9am (UTC)")
        probe = self.recheck(h, "new", verdict=limited)
        probe.assert_called_once()
        self.assertEqual(h["backoffReason"], "usage_limit")
        self.assertEqual(h["backoffAccount"], "new")
        self.assertNotEqual(h["backoffUntil"], inbox.iso(self.until))
        # Limited too: the next checks find the account it recorded.
        probe = self.recheck(h, "new", at=self.now + inbox.ACCOUNT_CHECK)
        probe.assert_not_called()

    def test_checks_wait_their_interval(self):
        h = self.paused()
        self.recheck(h, "old")
        probe = self.recheck(h, "new", at=self.now + inbox.ACCOUNT_CHECK / 2)
        probe.assert_not_called()
        probe = self.recheck(h, "new", at=self.now + inbox.ACCOUNT_CHECK)
        probe.assert_called_once()

    def test_a_failed_probe_holds_the_next_one(self):
        h = self.paused()
        self.recheck(h, "new", verdict=("error", "network"))
        self.assertEqual(h["backoffReason"], "usage_limit")
        self.assertIn("probe_failed", self.log_events())
        probe = self.recheck(h, "new", at=self.now + inbox.ACCOUNT_CHECK)
        probe.assert_not_called()
        probe = self.recheck(h, "new", at=self.now + inbox.PROBE_RETRY)
        probe.assert_called_once()

    def test_other_pauses_are_left_alone(self):
        for reason in ("budget", "signed_out", "memory_pressure"):
            h = self.paused(reason=reason)
            self.runner.account_checked_at = None
            probe = self.recheck(h, "new")
            probe.assert_not_called()
            self.assertEqual(h["backoffReason"], reason)

    def test_an_answer_lifts_the_needs_hold_a_limit_set(self):
        h = self.paused()
        self.runner.needs_judge_hold = self.now + inbox.MACHINE_PAUSE
        self.runner.needs_judge_hold_reason = "usage_limit"
        self.recheck(h, "new")
        self.assertIsNone(self.runner.needs_judge_hold)
        # A hold another machine failure set stays.
        h = self.paused()
        self.runner.account_checked_at = None
        self.runner.needs_judge_hold = self.now + inbox.MACHINE_PAUSE
        self.runner.needs_judge_hold_reason = "signed_out"
        self.recheck(h, "newer")
        self.assertIsNotNone(self.runner.needs_judge_hold)

    def test_signed_out_spends_no_probe(self):
        h = self.paused()
        probe = self.recheck(h, None)
        probe.assert_not_called()


class Probe(HomeCase):
    """probe() against canned `claude -p --output-format json` answers."""

    def answer(self, stdout, returncode=0, stderr=""):
        runner = inbox.Runner(inbox.Config(dict(os.environ), "org_test"))
        runner.state.ensure()
        done = mock.Mock(returncode=returncode, stdout=json.dumps(stdout) if stdout is not None else "",
                         stderr=stderr)
        with mock.patch.object(inbox, "text_run", return_value=done) as run:
            verdict = runner.probe()
        return verdict, run.call_args[0][0]

    def test_an_answer_is_ok(self):
        verdict, argv = self.answer({"type": "result", "subtype": "success", "is_error": False, "result": "OK"})
        self.assertEqual(verdict, ("ok", None))
        # No tools and one turn; the prompt follows an option, never --tools.
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--tools") + 2], "--max-turns")
        self.assertEqual(argv[-1], "Reply with the word OK.")

    def test_a_cap_still_means_the_turn_was_served(self):
        verdict, _ = self.answer({"type": "result", "subtype": "error_max_budget_usd", "is_error": True})
        self.assertEqual(verdict, ("ok", None))

    def test_a_limit_is_reported_with_its_message(self):
        verdict, _ = self.answer({"type": "result", "subtype": "success", "is_error": True,
                                  "result": "You've hit your weekly limit · resets Nov 4, 9am"}, returncode=1)
        self.assertEqual(verdict[0], "usage_limit")
        self.assertIn("resets Nov 4", verdict[1])

    def test_anything_else_is_an_error(self):
        verdict, _ = self.answer(None, returncode=1, stderr="connect ECONNREFUSED")
        self.assertEqual(verdict[0], "error")


if __name__ == "__main__":
    unittest.main()
