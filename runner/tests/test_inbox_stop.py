"""The stop file: how unio-runner stops a profile's runner where no signal
reaches it (Windows), so the runner ends as on SIGTERM rather than killed."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from inbox_support import RUNNER, FakeUnio, HomeCase, inbox


class StopFile(HomeCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        self.profile("org_test", FakeUnio.RUNNER, {})
        self.state = Path(inbox.profile_dirs("org_test")[1])
        self.state.mkdir(parents=True)

    def events(self):
        try:
            text = (self.state / "log.jsonl").read_text(encoding="utf-8")
        except OSError:
            return []
        return [json.loads(line) for line in text.splitlines() if line]

    def wait_for(self, event, proc, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            found = [e for e in self.events() if e.get("event") == event]
            if found:
                return found[-1]
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        self.fail("no %s line; log: %s" % (event, self.events()))

    def start(self):
        env = dict(os.environ, UNIO_API_URL=self.fake.url, UNIO_INBOX_CLAUDE=sys.executable)
        proc = subprocess.Popen([sys.executable, str(RUNNER), "--workspace", "org_test"], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return proc

    def test_the_runner_stops_as_on_a_signal(self):
        # One left from before is a stop already done, never one for this process.
        (self.state / inbox.STOP_FILE).write_text("", encoding="utf-8")
        proc = self.start()
        self.wait_for("start", proc)
        time.sleep(1.5)
        self.assertIsNone(proc.poll(), "a stale stop file stopped the runner")
        (self.state / inbox.STOP_FILE).write_text("", encoding="utf-8")
        self.assertEqual(proc.wait(timeout=60), 0)
        stop = self.wait_for("stop", proc)
        self.assertEqual(stop.get("by"), "stop_file")
        self.assertFalse((self.state / inbox.STOP_FILE).exists())
        # It said its presence ended, as a stopped service does.
        states = [r["body"].get("state") for r in self.fake.seen("PUT") if r["path"].startswith("/v1/agents/presence/")]
        self.assertEqual(states[-1], "ended")


if __name__ == "__main__":
    unittest.main()
