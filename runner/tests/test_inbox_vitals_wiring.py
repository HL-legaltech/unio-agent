"""How the runner drives Vitals: built from the config or injected, the
refresh bracketed before the claim, each run opened and closed around its
sessions, the sampler started by the service alone and stopped before the
presence ends."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from inbox_support import RUNNER, FakeUnio, HomeCase, inbox, write_fake_claude
from test_inbox_e2e import TASK_ID, mention


class StubVitals:
    """Vitals' interface, recording each call. `summary` is what run_end()
    returns; `on_start` runs inside run_start(), as a cause found then would."""

    def __init__(self, summary=None, on_start=None):
        self.calls = []
        self.on_change = None
        self.summary = summary
        self.on_start = on_start
        self.presence = None

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop", self.presence.closing if self.presence else None))

    def stage(self, name):
        self.calls.append(("stage", name))

    def run_start(self):
        self.calls.append(("run_start",))
        if self.on_start:
            self.on_start(self)

    def note_retries(self, credits):
        self.calls.append(("note_retries", list(credits)))

    def run_end(self):
        self.calls.append(("run_end",))
        return self.summary

    def names(self):
        return [c[0] if c[0] != "stage" else f"stage:{c[1]}" for c in self.calls]


class Construction(HomeCase):
    def setUp(self):
        super().setUp()
        self.profile("org_test", FakeUnio.RUNNER, {})

    def cfg(self, **env):
        return inbox.Config(dict(os.environ, **env), "org_test")

    def test_off_builds_none(self):
        self.assertIsNone(inbox.Runner(self.cfg(UNIO_INBOX_VITALS="off")).vitals)

    def test_on_builds_one_wired_to_the_presence(self):
        runner = inbox.Runner(self.cfg(UNIO_INBOX_VITALS="on"))
        self.assertIsInstance(runner.vitals, inbox.Vitals)
        self.assertEqual(runner.vitals.on_change, runner.presence.refresh)
        self.assertIsNone(runner.vitals.thread, "nothing samples before serve()")

    def test_an_injected_one_wins_over_the_config(self):
        stub = StubVitals()
        runner = inbox.Runner(self.cfg(UNIO_INBOX_VITALS="off"), vitals=stub)
        self.assertIs(runner.vitals, stub)
        self.assertEqual(stub.on_change, runner.presence.refresh)


class Serve(HomeCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        self.profile("org_test", FakeUnio.RUNNER, {})
        os.environ["UNIO_API_URL"] = self.fake.url
        self.addCleanup(inbox.stopping.clear)

    def test_the_service_starts_the_sampler_and_stops_it_before_the_presence(self):
        stub = StubVitals()
        runner = inbox.Runner(inbox.Config(os.environ, "org_test"), vitals=stub)
        stub.presence = runner.presence
        with mock.patch.object(runner, "locked_poll", side_effect=lambda: inbox.stopping.set()):
            self.assertEqual(runner.serve(), 0)
        self.assertEqual(stub.calls, [("start",), ("stop", False)])

    def test_once_runs_no_sampler(self):
        stub = StubVitals()
        runner = inbox.Runner(inbox.Config(os.environ, "org_test"), vitals=stub)
        with mock.patch.object(inbox, "desk_memory", return_value=None):
            runner.locked_poll(once=True)
        self.assertNotIn("start", stub.names())


class Run(HomeCase):
    """One item through handle(), with a recording Vitals."""

    def setUp(self):
        super().setUp()
        self.fake = FakeUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        self.fake.items = [mention()]
        bin_dir = self.tmp / "bin"
        write_fake_claude(bin_dir)
        os.environ.update(UNIO_API_URL=self.fake.url, FAKE_CLAUDE_LOG=str(self.tmp / "claude.jsonl"),
                          PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        os.environ.pop("UNIO_INBOX_CLAUDE", None)
        self.profile("org_test", FakeUnio.RUNNER, {"ABC": []})
        self.state = Path(inbox.profile_dirs("org_test")[1])

    def once(self, stub):
        runner = inbox.Runner(inbox.Config(os.environ, "org_test"), vitals=stub)
        with mock.patch.object(inbox, "desk_memory", return_value=None):
            self.assertEqual(runner.locked_poll(once=True), 1)
        return runner

    def items(self):
        return [json.loads(line) for line in (self.state / "log.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line)["event"] == "item"]

    def test_the_refresh_is_bracketed_and_the_run_opened_and_closed(self):
        stub = StubVitals(summary={"wallS": 3.0, "slow": ["slow_unio"], "causeS": {}, "stall": {}, "psi": {}})
        self.once(stub)
        # note_retries comes from the wait loop, which a quick fake run may never reach.
        names = [n for n in stub.names() if n != "note_retries"]
        self.assertEqual(names, ["stage:refresh", "stage:None", "run_start", "run_end"])
        item, = self.items()
        self.assertEqual(item["slow"], ["slow_unio"])

    def test_the_ai_services_retries_reach_vitals_while_the_run_waits(self):
        os.environ["FAKE_CLAUDE_MODE"] = "retry"
        stub = StubVitals()
        self.once(stub)
        credits = [c[1] for c in stub.calls if c[0] == "note_retries"]
        self.assertTrue(credits)
        self.assertEqual([(c["code"], c["delayMs"]) for c in credits[-1]], [("slow_ai", 1500)])

    def test_a_refresh_that_raises_still_closes_the_stage(self):
        stub = StubVitals()
        runner = inbox.Runner(inbox.Config(os.environ, "org_test"), vitals=stub)
        with mock.patch.object(runner.snapshots, "for_team", side_effect=RuntimeError("git")), \
                mock.patch.object(inbox, "desk_memory", return_value=None), self.assertRaises(RuntimeError):
            runner.locked_poll(once=True)
        self.assertEqual(stub.names()[:2], ["stage:refresh", "stage:None"])
        self.assertNotIn("run_start", stub.names())

    def test_the_working_body_carries_the_cause_vitals_reports(self):
        def found(stub):
            stub.on_change("slow_disk", "Disco em espera 41% do tempo")
        stub = StubVitals(on_start=found)
        self.once(stub)
        working = [r["body"] for r in self.fake.seen("PUT") if r["path"].startswith("/v1/agents/presence/")
                   and r["body"].get("state") == "working"]
        self.assertTrue(working)
        self.assertEqual({(b.get("reason"), b.get("message")) for b in working},
                         {("slow_disk", "Disco em espera 41% do tempo")})
        self.assertEqual(working[0]["taskId"], TASK_ID)


class RealSampler(HomeCase):
    """The service with this system's own readers: it writes vitals.json and
    stops cleanly. A smoke test on each system CI runs."""

    def test_the_service_writes_vitals_json(self):
        with FakeUnio() as fake:
            self.profile("org_test", FakeUnio.RUNNER, {})
            state = Path(inbox.profile_dirs("org_test")[1])
            state.mkdir(parents=True)
            env = dict(os.environ, UNIO_API_URL=fake.url, UNIO_INBOX_CLAUDE=sys.executable, UNIO_INBOX_VITALS="on")
            proc = subprocess.Popen([sys.executable, str(RUNNER), "--workspace", "org_test"], env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not (state / "vitals.json").exists():
                time.sleep(0.1)
            (state / inbox.STOP_FILE).write_text("", encoding="utf-8")
            self.assertEqual(proc.wait(timeout=60), 0)
        vitals = json.loads((state / "vitals.json").read_text(encoding="utf-8"))
        self.assertEqual(vitals["platform"], inbox.platform_name())
        self.assertIn("disk", vitals["signals"])
        log = [json.loads(line) for line in (state / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e for e in log if e["event"] == "vitals_error"], [])

