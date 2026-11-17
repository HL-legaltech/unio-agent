"""What makes one answer slow outside the computer: Unio's calls timed and
sorted, and the AI service's retries counted per run."""
import errno
import io
import json
import socket
import unittest
import urllib.error
from unittest import mock

from inbox_support import inbox


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def health():
    clock = Clock()
    return inbox.ApiHealth(clock), clock


class FailureKinds(unittest.TestCase):
    def test_connect_failures_are_the_network_or_unio(self):
        kind = inbox.unreachable_kind
        self.assertEqual(kind(urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))),
                         ("network", True))
        self.assertEqual(kind(urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable"))),
                         ("network", True))
        self.assertEqual(kind(urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route to host"))),
                         ("network", True))
        self.assertEqual(kind(urllib.error.URLError(socket.timeout("timed out"))), ("network", False))
        self.assertEqual(kind(urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))),
                         ("unio", False))

    def test_a_bare_timeout_is_a_slow_read(self):
        self.assertEqual(inbox.unreachable_kind(socket.timeout("timed out")), ("slow", False))
        self.assertEqual(inbox.unreachable_kind(TimeoutError("timed out")), ("slow", False))

    def test_anything_else_has_no_kind(self):
        self.assertEqual(inbox.unreachable_kind(ConnectionResetError(errno.ECONNRESET, "reset")), (None, False))
        self.assertEqual(inbox.unreachable_kind(urllib.error.URLError("unknown url type")), (None, False))


class UnioCauses(unittest.TestCase):
    def test_slow_network_needs_two_failures_beside_an_answer(self):
        h, clock = health()
        h.record("network", 0.1)
        h.record("network", 0.1)
        self.assertEqual(h.causes(), {})
        h.record("ok", 0.2, 200)
        self.assertIn("slow_network", h.causes())

    def test_a_server_error_is_not_the_answer_slow_network_needs(self):
        h, clock = health()
        h.record("network", 0.1)
        h.record("network", 0.1)
        h.record("ok", 0.2, 502)
        self.assertNotIn("slow_network", h.causes())

    def test_failures_older_than_two_minutes_do_not_count(self):
        h, clock = health()
        h.record("network", 0.1)
        clock.now += 121
        h.record("network", 0.1)
        h.record("ok", 0.2, 200)
        self.assertEqual(h.causes(), {})

    def test_slow_unio_from_the_p90_of_the_last_ten(self):
        h, clock = health()
        for _ in range(4):
            h.record("ok", 4.0, 200)
        self.assertEqual(h.causes(), {})
        h.record("ok", 4.0, 200)
        self.assertIn("slow_unio", h.causes())

    def test_read_timeouts_count_at_their_timeout(self):
        h, clock = health()
        for _ in range(5):
            h.record("slow", 0.0, timeout=3)
        self.assertIn("slow_unio", h.causes())
        self.assertEqual(h.p90(), 3)

    def test_slow_unio_from_server_errors_or_refusals_beside_a_good_answer(self):
        h, clock = health()
        h.record("ok", 0.1, 502)
        h.record("unio", 0.0)
        self.assertEqual(h.causes(), {})
        h.record("ok", 0.1, 200)
        self.assertIn("slow_unio", h.causes())

    def test_five_fast_answers_in_a_row_clear_both(self):
        h, clock = health()
        h.record("network", 0.1)
        h.record("network", 0.1)
        for _ in range(5):
            h.record("ok", 5.0, 200)
        self.assertEqual(set(h.causes()), {"slow_network", "slow_unio"})
        for _ in range(4):
            h.record("ok", 0.2, 200)
        h.record("ok", 1.5, 200)
        for _ in range(4):
            h.record("ok", 0.2, 200)
        self.assertTrue(h.causes())
        h.record("ok", 0.2, 200)
        self.assertEqual(h.causes(), {})
        # The old slow answers are forgotten, so the cause does not come straight back.
        h.record("ok", 0.2, 200)
        self.assertEqual(h.causes(), {})

    def test_a_cause_whose_condition_is_gone_clears_after_ten_calls(self):
        h, clock = health()
        for _ in range(5):
            h.record("ok", 3.5, 200)
        # The p90 falls under 3 s at the ninth 1.2 s answer; ten more clear it.
        for _ in range(17):
            h.record("ok", 1.2, 200)
        self.assertIn("slow_unio", h.causes())
        h.record("ok", 1.2, 200)
        self.assertEqual(h.causes(), {})

    def test_a_bad_call_of_its_own_restarts_the_quiet_count(self):
        h, clock = health()
        h.record("network", 0.1)
        h.record("network", 0.1)
        h.record("ok", 1.2, 200)
        clock.now += 121
        for _ in range(9):
            h.record("ok", 1.2, 200)
        h.record("network", 0.1)
        for _ in range(9):
            h.record("ok", 1.2, 200)
        self.assertIn("slow_network", h.causes())
        h.record("ok", 1.2, 200)
        self.assertEqual(h.causes(), {})

    def test_failures_of_no_known_kind_never_clear_a_cause(self):
        h, clock = health()
        h.record("network", 0.1)
        h.record("network", 0.1)
        h.record("ok", 1.2, 200)
        clock.now += 121
        for _ in range(9):
            h.record("ok", 1.2, 200)
        # Resets and TLS errors are of no known kind: no sign the network is well.
        for _ in range(20):
            h.record(None, 0.1)
        self.assertIn("slow_network", h.causes())
        h.record("ok", 1.2, 200)
        self.assertEqual(h.causes(), {})

    def test_during_a_bot_stall_timeouts_are_the_machines(self):
        h, clock = health()
        h.stalled = True
        for _ in range(6):
            h.record("slow", 0.0, timeout=3)
            h.record("network", 3.0, definite=False)
        h.record("ok", 0.2, 200)
        self.assertEqual(h.causes(), {})
        h.record("network", 0.1, definite=True)
        h.record("network", 0.1, definite=True)
        self.assertEqual(set(h.causes()), {"slow_network"})

    def test_the_latest_bad_call_is_kept_per_cause(self):
        h, clock = health()
        h.record("network", 0.1)
        h.record("network", 0.1)
        h.record("ok", 0.2, 200)
        first = h.causes()["slow_network"]
        h.record("network", 0.1)
        self.assertGreater(h.causes()["slow_network"], first)

    def test_network_recent(self):
        h, clock = health()
        self.assertFalse(h.network_recent())
        h.record("network", 0.1)
        clock.now += 120
        self.assertTrue(h.network_recent())
        clock.now += 1
        self.assertFalse(h.network_recent())


class Answer:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


class CallSampling(unittest.TestCase):
    def setUp(self):
        self.health, _ = health()
        patcher = mock.patch.object(inbox, "API_HEALTH", self.health)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.kinds = []
        real = self.health.record
        self.health.record = lambda kind, *a, **k: (self.kinds.append(kind), real(kind, *a, **k))[1]
        self.api = inbox.Api("http://127.0.0.1:9/v1", "t")

    def urlopen(self, error=None):
        def fake(req, timeout):
            if error:
                raise error
            return Answer()
        return mock.patch.object(inbox.urllib.request, "urlopen", fake)

    def test_answers_and_failures_are_timed_with_their_kind(self):
        with self.urlopen():
            self.assertEqual(self.api.call("GET", "/x"), (200, {}))
        with self.urlopen(socket.timeout("timed out")):
            with self.assertRaises(inbox.ApiUnreachable) as caught:
                self.api.call("GET", "/x", timeout=3)
        self.assertEqual(caught.exception.kind, "slow")
        with self.urlopen(urllib.error.URLError(socket.gaierror(-2, "no name"))):
            with self.assertRaises(inbox.ApiUnreachable) as caught:
                self.api.call("GET", "/x")
        self.assertEqual(caught.exception.kind, "network")
        self.assertEqual(self.kinds, ["ok", "slow", "network"])

    def test_uploads_and_settle_retries_are_not_sampled(self):
        with self.urlopen():
            self.api.call("POST", "/files", {}, timeout=60)
        self.assertEqual(self.kinds, [])
        with self.urlopen(urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))), \
                mock.patch.object(inbox.time, "sleep"):
            status, data = self.api.settle("POST", "/agents/inbox/complete", {}, "k")
        self.assertEqual(status, 0)
        self.assertEqual(self.kinds, ["unio"])

    def test_a_failing_hook_never_breaks_a_call(self):
        def broken(*args, **kwargs):
            raise RuntimeError("boom")
        self.health.record = broken
        with self.urlopen():
            self.assertEqual(self.api.call("GET", "/x"), (200, {}))
        self.assertEqual(self.health.hook_error, "RuntimeError: boom")


def retry(error, status, delay=1000, **extra):
    return {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10, "retry_delay_ms": delay,
            "error_status": status, "error": error, "session_id": "s", **extra}


class RetryCredit(unittest.TestCase):
    def test_overload_and_server_errors_are_the_ai_service(self):
        for error, status in (("overloaded", 529), ("server_error", 500), ("unknown", 418)):
            self.assertEqual(inbox.retry_cause(retry(error, status)), "slow_ai")

    def test_limits_and_sign_in_are_problems_of_their_own(self):
        for error, status in (("rate_limit", 429), ("authentication_failed", 401), ("cloud_credential_error", 403),
                              ("rate_limit", None)):
            self.assertIsNone(inbox.retry_cause(retry(error, status)))

    def test_a_connection_error_is_the_network_only_when_unio_calls_failed_too(self):
        self.assertEqual(inbox.retry_cause(retry("unknown", None)), "slow_ai")
        self.assertEqual(inbox.retry_cause(retry("unknown", None), network_recent=True), "slow_network")


class Proc:
    def __init__(self, events):
        self.stdout = [(json.dumps(e) + "\n").encode() for e in events]


class RetryCounting(unittest.TestCase):
    def read(self, events, network=False):
        s = inbox.Session()
        h = inbox.ApiHealth()
        if network:
            h.record("network", 0.1)
        with mock.patch.object(inbox, "API_HEALTH", h):
            inbox.Runner._read(Proc(events), s, io.BytesIO())
        return s

    def test_retries_are_counted_on_the_session(self):
        s = self.read([retry("overloaded", 529, 2000), retry("rate_limit", 429, 50000),
                       retry("unknown", None, 500), retry("overloaded", 529, "soon"),
                       {"type": "result", "subtype": "success", "result": "ok"}])
        self.assertEqual(s.retries, 4)
        self.assertEqual(s.retry_errors, {"overloaded": 2, "rate_limit": 1, "unknown": 1})
        self.assertEqual(s.retry_delay_ms, 52500)
        self.assertEqual([(c["code"], c["delayMs"]) for c in s.retry_credits],
                         [("slow_ai", 2000), ("slow_ai", 500), ("slow_ai", 0)])
        self.assertEqual(s.result["subtype"], "success")

    def test_a_connection_error_after_unio_network_failures_credits_the_network(self):
        s = self.read([retry("unknown", None, 800)], network=True)
        self.assertEqual([c["code"] for c in s.retry_credits], ["slow_network"])

    def test_a_subagents_retry_is_not_the_runs(self):
        s = self.read([retry("overloaded", 529, parent_tool_use_id="toolu_1")])
        self.assertEqual(s.retries, 0)


if __name__ == "__main__":
    unittest.main()
