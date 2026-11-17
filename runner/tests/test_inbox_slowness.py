"""What makes one answer slow outside the computer: Unio's calls timed and
sorted, and the AI service's retries counted per run."""
import errno
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


if __name__ == "__main__":
    unittest.main()
