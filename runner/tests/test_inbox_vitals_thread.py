"""The Vitals sampler as the runner will drive it: fake readings and a fake
clock in, causes, the owner's detail, run summaries and vitals.json out."""
import json
import threading
import time
import unittest

from inbox_support import HomeCase, inbox

GB = 2**30


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeReader(inbox.VitalsReader):
    """PSI counters growing at the shares set on it, plus any fixed readings."""

    def __init__(self, state_dir, clock):
        super().__init__(state_dir)
        self.clock = clock
        self.share = {"memory": 0.0, "io": 0.0, "cpu": 0.0}
        self.psi = {"memory": 0.0, "io": 0.0, "cpu": 0.0}
        self.fixed = {"mem": {"avail": 20 * GB, "total": 32 * GB, "swapTotal": 8 * GB, "swapFree": 8 * GB},
                      "disk": {"free": 100 * GB, "total": 200 * GB}}
        self.last = None
        self.boom = None

    def sample(self):
        if self.boom:
            raise self.boom
        self.got = {"psi", "mem", "disk"}
        now = self.clock()
        if self.last is not None:
            for res, share in self.share.items():
                self.psi[res] += share * (now - self.last)
        self.last = now
        return {"psiSource": "cgroup", "psi": dict(self.psi), **self.fixed}


class Case(HomeCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.health = inbox.ApiHealth(self.clock)
        self.reader = FakeReader(self.tmp, self.clock)
        self.logs = []
        self.changes = []
        self.scans = []
        self.vitals = self.make()

    def make(self, env=None, scan="default"):
        def fake_scan():
            self.scans.append(self.clock.now)
            return {"node": 7 * GB, "claude": 3 * GB}
        return inbox.Vitals(self.tmp, log=lambda event, **f: self.logs.append((event, f)), platform="linux",
                            reader=self.reader, clock=self.clock, env=env or {}, health=self.health,
                            scan=fake_scan if scan == "default" else scan,
                            on_change=lambda reason, message: self.changes.append((reason, message)))

    def tick(self, n=1, seconds=5):
        for _ in range(n):
            self.clock.now += seconds
            self.vitals.tick()

    def events(self, name):
        return [f for e, f in self.logs if e == name]


class NeverRaises(Case):
    def test_a_failing_reader_is_logged_once_and_the_tick_goes_on(self):
        self.reader.boom = RuntimeError("no /proc")
        self.tick(3)
        self.assertEqual(self.events("vitals_error"), [{"what": "tick", "error": "RuntimeError: no /proc"}])
        self.reader.boom = None
        self.tick(1)

    def test_a_signal_switched_off_is_logged_once(self):
        self.reader.off["psi"] = "FileNotFoundError: x"
        self.tick(3)
        self.assertEqual(self.events("vitals_signal_off"), [{"signal": "psi", "error": "FileNotFoundError: x"}])

    def test_a_failing_on_change_never_stops_the_sampler(self):
        def broken(reason, message):
            raise RuntimeError("presence down")
        self.vitals.on_change = broken
        self.reader.fixed["disk"] = {"free": GB, "total": 200 * GB}
        self.tick(3)
        self.assertEqual([f["what"] for f in self.events("vitals_error")], ["on_change"])
        self.assertEqual(self.vitals.reason()[0], "low_disk_space")

    def test_a_failing_api_hook_is_logged_once(self):
        self.health.hook_error = "RuntimeError: boom"
        self.tick(2)
        self.assertEqual(self.events("vitals_error"), [{"what": "api_timing", "error": "RuntimeError: boom"}])


class Idle(Case):
    def test_a_bot_stall_is_no_idle_reason(self):
        self.reader.share["io"] = 0.5
        self.tick(3)
        self.assertIn("slow_disk", self.vitals.snapshot()["causes"])
        self.assertEqual(self.changes, [])
        self.assertTrue(self.health.stalled)

    def test_low_disk_space_is_reported_with_the_owners_detail_and_cleared(self):
        self.reader.fixed["disk"] = {"free": 1.5 * GB, "total": 200 * GB}
        self.tick(2)
        self.assertEqual(self.changes, [("low_disk_space", "Disco: 1,5 de 200,0 GB livres")])
        self.assertEqual([f["code"] for f in self.events("slow_start")], ["low_disk_space"])
        self.reader.fixed["disk"] = {"free": 50 * GB, "total": 200 * GB}
        self.tick(62)
        self.assertEqual(self.changes[-1], (None, None))
        self.assertEqual(self.events("slow_end")[0]["code"], "low_disk_space")

    def test_a_long_refresh_is_updating_code_until_it_returns(self):
        self.tick(1)
        self.vitals.stage("refresh")
        self.tick(2)
        self.assertEqual(self.changes, [])
        self.tick(1)
        self.assertEqual(self.changes, [("updating_code", "Atualizando a cópia do código")])
        self.vitals.stage(None)
        self.assertEqual(self.changes[-1], (None, None))


class Working(Case):
    def test_the_bots_own_stall_shows_at_once(self):
        self.tick(1)
        self.vitals.run_start()
        self.reader.share["memory"] = 0.5
        self.tick(2)
        reason, message = self.changes[-1]
        self.assertEqual(reason, "slow_memory")
        self.assertIn("Memória: 20,0 de 32,0 GB livres, swap 0%", message)
        self.assertIn("Mais pesados: node 7,0 GB, claude 3,0 GB", message)

    def test_a_cause_without_evidence_waits_a_minute(self):
        self.reader.fixed["disk"] = {"free": GB, "total": 200 * GB}
        self.tick(2)
        self.vitals.run_start()
        self.tick(11)
        self.assertIsNone(self.vitals.reason()[0])
        self.tick(1)
        # Idle shows it at once; a run only once it is 60 s old.
        self.assertEqual([r for r, _ in self.changes], ["low_disk_space", None, "low_disk_space"])

    def test_a_slow_unio_seen_before_the_claim_waits_a_minute(self):
        for _ in range(5):
            self.health.record("ok", 4.0, 200)
        self.vitals.run_start()
        self.tick(2)
        self.assertIsNone(self.vitals.reason()[0])
        self.health.record("ok", 5.0, 200)
        self.tick(1)
        self.assertEqual(self.vitals.reason()[0], "slow_unio")

    def test_a_credited_retry_shows_at_once_and_holds_120_s_after_the_run(self):
        self.vitals.run_start()
        self.vitals.note_retries([{"code": "slow_ai", "delayMs": 5000, "at": self.clock.now}])
        self.assertEqual(self.changes[-1], ("slow_ai", "Serviço de IA: 1 nova tentativa"))
        self.tick(2)
        summary = self.vitals.run_end()
        self.assertEqual(summary["slow"], ["slow_ai"])
        self.assertEqual(self.changes[-1], (None, None))
        self.vitals.run_start()
        self.tick(12)
        self.assertEqual(self.vitals.reason()[0], "slow_ai")
        self.tick(13)
        self.assertIsNone(self.vitals.reason()[0])

    def test_a_bot_stall_goes_ahead_of_slow_unio(self):
        self.tick(1)
        self.vitals.run_start()
        for _ in range(5):
            self.health.record("ok", 4.0, 200)
        self.reader.share["io"] = 0.5
        self.tick(2)
        self.assertEqual(self.vitals.reason()[0], "slow_disk")


class RunSummary(Case):
    def test_the_summary_holds_what_the_run_saw(self):
        self.tick(1)
        self.vitals.run_start()
        self.reader.share["memory"] = 0.4
        self.tick(20)
        s = self.vitals.run_end()
        self.assertEqual(s["wallS"], 100.0)
        self.assertEqual(s["slow"], ["slow_memory"])
        self.assertEqual(s["causeS"], {"slow_memory": 95})
        self.assertEqual(s["stall"], {"memory": 0.4, "io": 0.0, "cpu": 0.0})
        self.assertEqual(s["psi"], {"source": "cgroup", "bot": {"memory": 40.0, "io": 0.0, "cpu": 0.0}})
        self.assertEqual(inbox.timeout_cause(s), "slow_memory")
        self.assertIsNone(self.vitals.run_end())

    def test_no_run_no_summary(self):
        self.assertIsNone(self.vitals.run_end())


class TimeoutCredit(unittest.TestCase):
    def test_a_stall_of_15_percent(self):
        run = {"wallS": 600, "slow": ["slow_disk", "slow_cpu"], "causeS": {"slow_disk": 30, "slow_cpu": 30},
               "stall": {"io": 0.15, "cpu": 0.1}}
        self.assertEqual(inbox.timeout_cause(run), "slow_disk")
        run["stall"]["io"] = 0.14
        self.assertIsNone(inbox.timeout_cause(run))

    def test_retries_waiting_a_minute(self):
        run = {"wallS": 600, "slow": ["slow_ai"], "causeS": {"slow_ai": 20}, "stall": {}}
        self.assertIsNone(inbox.timeout_cause(run, ai_delay_ms=59999))
        self.assertEqual(inbox.timeout_cause(run, ai_delay_ms=60000), "slow_ai")
        self.assertEqual(inbox.ai_retry_delay([{"code": "slow_ai", "delayMs": 40000},
                                               {"code": "slow_network", "delayMs": 90000},
                                               {"code": "slow_ai", "delayMs": 20000}]), 60000)

    def test_held_for_a_quarter_of_the_run(self):
        run = {"wallS": 600, "slow": ["slow_unio", "slow_cpu"], "causeS": {"slow_unio": 149, "slow_cpu": 150},
               "stall": {}}
        self.assertEqual(inbox.timeout_cause(run), "slow_cpu")
        run["causeS"]["slow_unio"] = 150
        self.assertEqual(inbox.timeout_cause(run), "slow_unio")

    def test_the_refresh_and_disk_space_never_explain_a_timeout(self):
        run = {"wallS": 600, "slow": ["updating_code", "low_disk_space"],
               "causeS": {"updating_code": 600, "low_disk_space": 600}, "stall": {}}
        self.assertIsNone(inbox.timeout_cause(run))
        self.assertIsNone(inbox.timeout_cause(None))


class Detail(Case):
    def test_jitter_does_not_change_the_detail_and_it_is_rebuilt_at_most_every_minute(self):
        self.reader.fixed["disk"] = {"free": 1.5 * GB, "total": 200 * GB}
        self.tick(2)
        self.reader.fixed["disk"] = {"free": 1.52 * GB, "total": 200 * GB}
        self.tick(5)
        self.reader.fixed["disk"] = {"free": 1.0 * GB, "total": 200 * GB}
        self.tick(5)
        self.assertEqual(len(self.changes), 1)
        self.tick(2)
        self.assertEqual(self.changes[-1], ("low_disk_space", "Disco: 1,0 de 200,0 GB livres"))

    def test_programs_are_scanned_at_the_start_and_at_most_every_minute(self):
        self.tick(1)
        self.vitals.run_start()
        self.reader.share["cpu"] = 0.5
        self.tick(30)
        self.assertEqual(len(self.scans), 3)
        self.assertEqual(self.scans[1] - self.scans[0], 60)

    def test_the_opt_out_names_no_programs(self):
        self.vitals = self.make(env={"UNIO_INBOX_SHARE_PROGRAMS": "0"})
        self.tick(1)
        self.vitals.run_start()
        self.reader.share["cpu"] = 0.5
        self.tick(3)
        self.assertEqual(self.scans, [])
        self.assertEqual(self.changes[-1], ("slow_cpu", "Processador em espera 50% do tempo"))


class VitalsFile(Case):
    def test_written_every_30_s_with_the_causes(self):
        self.reader.fixed["disk"] = {"free": GB, "total": 200 * GB}
        self.tick(1)
        first = json.loads((self.tmp / "vitals.json").read_text(encoding="utf-8"))
        self.assertEqual(first["causes"], {})
        self.tick(5)
        self.assertEqual(json.loads((self.tmp / "vitals.json").read_text(encoding="utf-8")), first)
        self.tick(1)
        data = json.loads((self.tmp / "vitals.json").read_text(encoding="utf-8"))
        self.assertEqual(list(data["causes"]), ["low_disk_space"])
        self.assertEqual(data["platform"], "linux")
        self.assertEqual(data["reason"], "low_disk_space")
        self.assertEqual(data["readings"]["diskFreeGB"], 1.0)
        self.assertEqual(sorted(data["signals"]), ["disk", "mem", "psi"])

    def test_no_folder_no_file(self):
        self.vitals = inbox.Vitals(self.tmp / "missing", reader=self.reader, clock=self.clock, health=self.health,
                                   scan=False)
        self.tick(1)
        self.assertFalse((self.tmp / "missing").exists())


class Thread(Case):
    def test_start_samples_at_once_and_stop_ends_the_thread(self):
        seen = threading.Event()
        self.reader.sample_real = self.reader.sample
        self.reader.sample = lambda: (seen.set(), self.reader.sample_real())[1]
        self.vitals.clock = time.monotonic
        self.vitals.start()
        self.assertTrue(seen.wait(5))
        self.vitals.stop()
        self.assertFalse(self.vitals.thread.is_alive())
        self.assertTrue((self.tmp / "vitals.json").exists())


class LogLock(HomeCase):
    def test_two_threads_log_every_line(self):
        state = inbox.State(inbox.Config(dict(__import__("os").environ)))
        state.ensure()

        def write(n):
            for i in range(200):
                state.log("x", n=n, i=i)
        threads = [threading.Thread(target=write, args=(n,)) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = state.log_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 400)
        self.assertTrue(all(json.loads(line)["event"] == "x" for line in lines))


if __name__ == "__main__":
    unittest.main()
