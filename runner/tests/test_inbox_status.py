"""The project status pass against a fake Unio API: which moves it makes and
which it holds back, what it logs, the --status dry run, and a profile that
leaves the pass off unless its env turns it on."""
import json
import os
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from inbox_support import FakeUnio, HomeCase, inbox

P1, P2, P3, P4 = ("11111111-1111-4111-8111-11111111111" + str(i) for i in range(1, 5))
IN_PROGRESS, COMPLETED = "22222222-2222-4222-8222-222222222221", "22222222-2222-4222-8222-222222222222"
STATUS_PATH = "/v1/agents/projects/status"


def row(pid, to, because, since, category="planned", key="ABC-1", slug="proj-p1"):
    """A row of GET /agents/projects/status, with a name the runner must never log."""
    return {"id": pid, "slug": slug, "name": "Project Marker",
            "status": {"id": None, "category": category, "setAt": since, "setBy": None},
            "advice": {"to": {"id": to, "category": "completed" if to == COMPLETED else "in_progress"},
                       "because": because, "since": since, "task": {"id": P4, "key": key}}}


class StatusUnio(FakeUnio):
    """FakeUnio plus the status route, and update_project refused for the
    projects in `refuse` the way the MCP endpoint refuses a tool call."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self.status_code = 200
        self.refuse = set()

    def route(self, request):
        method, path = request["method"], request["path"]
        if request["token"] in (self.RUNNER, self.SESSION):
            if method == "GET" and path == STATUS_PATH:
                if self.status_code != 200:
                    return self.status_code, {"error": "Não encontrado.", "code": "not_found"}
                return 200, {"data": list(self.rows)}
            body = request["body"] if isinstance(request["body"], dict) else {}
            params = body.get("params") or {}
            if method == "POST" and path == "/v1/mcp" and params.get("name") == "update_project":
                project = params["arguments"]["project"]
                if project in self.refuse:
                    result = {"isError": True, "content": [{"type": "text", "text": "No project status \"x\"."}]}
                else:
                    answer = {"id": project, "changed": ["status"]}
                    result = {"content": [{"type": "text", "text": json.dumps(answer)}]}
                return 200, {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
        return super().route(request)

    def moves(self):
        return [(r["body"]["params"]["arguments"]["project"], r["body"]["params"]["arguments"]["status"])
                for r in self.seen("POST", "/v1/mcp") if r["body"]["params"]["name"] == "update_project"]


class StatusCase(HomeCase):
    def setUp(self):
        super().setUp()
        self.fake = StatusUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        os.environ["UNIO_API_URL"] = self.fake.url
        for key in ("UNIO_STATUS", "UNIO_STATUS_INTERVAL", "UNIO_STATUS_DONE_AFTER"):
            os.environ.pop(key, None)
        self.now = inbox.utcnow()

    def ago(self, hours):
        return inbox.iso(self.now - timedelta(hours=hours))

    def legacy_token(self):
        folder = self.home / ".config" / "unio"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "token").write_text(FakeUnio.SESSION + "\n", encoding="utf-8")


class Switch(HomeCase):
    def test_off_unless_turned_on(self):
        for workspace in (None, "org_test"):
            with self.subTest(workspace=workspace):
                cfg = inbox.Config(dict(os.environ), workspace)
                self.assertEqual((cfg.status_mode, cfg.status_error), ("off", None))
                cfg = inbox.Config(dict(os.environ, UNIO_STATUS="on"), workspace)
                self.assertEqual((cfg.status_mode, cfg.status_error), ("on", None))
                self.assertEqual(cfg.status_interval, timedelta(minutes=15))
                self.assertEqual(cfg.status_done_after, timedelta(hours=24))

    def test_a_bad_value_is_a_config_error(self):
        bad = inbox.Config(dict(os.environ, UNIO_STATUS="maybe"))
        self.assertEqual(bad.status_mode, "off")
        self.assertIn("UNIO_STATUS=maybe", bad.status_error)
        for name, value in (("UNIO_STATUS_DONE_AFTER", "-1"), ("UNIO_STATUS_INTERVAL", "0")):
            with self.subTest(name=name):
                self.assertIn(name, inbox.Config(dict(os.environ, UNIO_STATUS="on", **{name: value})).status_error)


class Pass(StatusCase):
    """The pass as the single-machine service runs it with UNIO_STATUS=on."""

    def setUp(self):
        super().setUp()
        self.legacy_token()
        os.environ["UNIO_STATUS"] = "on"
        self.runner = self.make_runner()
        self.api = inbox.Api(self.runner.cfg.api, FakeUnio.SESSION)

    def make_runner(self):
        runner = inbox.Runner(inbox.Config(dict(os.environ)))
        runner.state.ensure()
        return runner

    def log(self):
        path = self.runner.state.log_file
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_due_moves_are_made_and_a_fresh_finish_waits(self):
        self.fake.rows = [
            row(P1, IN_PROGRESS, "work_started", self.ago(3)),
            row(P2, COMPLETED, "work_done", self.ago(2), category="in_progress", slug="proj-p2", key="ABC-2"),
            row(P3, COMPLETED, "work_done", self.ago(30), category="in_progress", slug="proj-p3", key="ABC-3"),
            {"id": "not-a-uuid", "advice": {}},
            row(P4, "nope", "work_started", self.ago(1), slug="proj-p4"),
        ]
        result = self.runner.status_pass(self.api, {}, self.now, "on")
        self.assertEqual(sorted(self.fake.moves()), sorted([(P1, IN_PROGRESS), (P3, COMPLETED)]))
        self.assertEqual((result["moved"], len(result["rows"]), result["error"]), (2, 3, None))
        entries = [e for e in self.log() if e["event"] == "status"]
        self.assertEqual(sorted((e["action"], e.get("project")) for e in entries),
                         sorted([("moved", "proj-p1"), ("moved", "proj-p3"), ("waiting", "proj-p2"), ("pass", None)]))
        waiting, = [e for e in entries if e["action"] == "waiting"]
        self.assertEqual(inbox.parse_iso(waiting["until"]), inbox.parse_iso(self.ago(2)) + timedelta(hours=24))
        text = self.runner.state.log_file.read_text(encoding="utf-8")
        self.assertIn("ABC-1", text)
        self.assertNotIn("Project Marker", text)
        health = json.loads(self.runner.state.health_file.read_text(encoding="utf-8"))
        self.assertEqual((health["status"]["mode"], health["status"]["pending"]), ("on", 1))

        # The finish still inside its day is neither tried nor logged again.
        self.fake.rows = [row(P2, COMPLETED, "work_done", self.ago(2), category="in_progress", slug="proj-p2")]
        before = len(self.fake.moves())
        self.runner.status_pass(self.api, {}, self.now, "on")
        self.assertEqual(len(self.fake.moves()), before)
        self.assertEqual(sum(1 for e in self.log() if e.get("action") == "waiting"), 1)

    def test_a_refused_move_waits_an_hour(self):
        self.fake.refuse = {P1}
        self.fake.rows = [row(P1, IN_PROGRESS, "work_started", self.ago(3))]
        self.runner.status_pass(self.api, {}, self.now, "on")
        self.runner.status_pass(self.api, {}, self.now + timedelta(minutes=20), "on")
        self.assertEqual(len(self.fake.moves()), 1)
        failed, = [e for e in self.log() if e.get("action") == "move_failed"]
        self.assertIn("No project status", failed["error"])
        self.fake.refuse = set()
        self.runner.status_pass(self.api, {}, self.now + timedelta(minutes=61), "on")
        self.assertEqual(len(self.fake.moves()), 2)

    def test_observe_writes_nothing_and_logs_a_decision_once(self):
        os.environ["UNIO_STATUS"] = "observe"
        observer = self.make_runner()
        self.fake.rows = [row(P1, IN_PROGRESS, "work_started", self.ago(3))]
        observer.status_pass(self.api, {}, self.now, "observe")
        observer.status_pass(self.api, {}, self.now + timedelta(minutes=15), "observe")
        self.assertEqual(self.fake.moves(), [])
        self.assertEqual(len([e for e in self.log() if e.get("action") == "would_move"]), 1)

    def test_a_dry_pass_writes_and_logs_nothing(self):
        self.fake.rows = [row(P1, IN_PROGRESS, "work_started", self.ago(3))]
        before = len(self.log())
        result = self.runner.status_pass(self.api, {}, self.now, "dry")
        self.assertEqual(result["rows"][0]["decision"], "move")
        self.assertEqual(self.fake.moves(), [])
        self.assertEqual(len(self.log()), before)

    def test_a_unio_without_the_route_is_logged_once(self):
        self.fake.status_code = 404
        first = self.runner.status_pass(self.api, {}, self.now, "on")
        self.runner.status_pass(self.api, {}, self.now + timedelta(minutes=15), "on")
        self.assertIn("does not serve it yet", first["error"])
        self.assertEqual(len([e for e in self.log() if e.get("action") == "error"]), 1)

    def test_the_tick_waits_its_interval(self):
        self.runner._status_tick(self.now)
        self.runner._status_tick(self.now + timedelta(minutes=5))
        self.assertEqual(self.runner.status_pass_at, self.now)
        self.assertEqual(len(self.fake.seen("GET", STATUS_PATH)), 1)


class ProfileDefault(StatusCase):
    """A profile's service, after a poll that found nothing: the server side of
    the pass is not everywhere yet, so only the profile's env turns it on."""

    def setUp(self):
        super().setUp()
        self.config = self.profile("org_test", FakeUnio.RUNNER, {"ABC": []})
        self.fake.rows = [row(P1, IN_PROGRESS, "work_started", self.ago(3))]

    def empty_poll(self):
        runner = inbox.Runner(inbox.Config(os.environ, "org_test"))
        with mock.patch.object(inbox, "desk_memory", return_value=None):
            listed = runner.locked_poll()
        runner.presence.close(ended=False)
        self.assertEqual(listed, 0)
        return runner

    def test_off_by_default(self):
        runner = self.empty_poll()
        self.assertEqual(runner.cfg.status_mode, "off")
        self.assertEqual(len(self.fake.seen("GET", "/v1/agents/inbox")), 1)
        self.assertEqual(self.fake.seen("GET", STATUS_PATH), [])
        self.assertEqual(self.fake.moves(), [])

    def test_the_env_file_turns_it_on(self):
        (self.config / "env").write_text("UNIO_STATUS=on\n", encoding="utf-8")
        inbox.apply_env_file(self.config / "env")
        self.empty_poll()
        self.assertEqual(len(self.fake.seen("GET", STATUS_PATH)), 1)
        self.assertEqual(self.fake.moves(), [(P1, IN_PROGRESS)])


class CommandLine(StatusCase):
    def test_status_is_a_dry_run_of_its_own(self):
        for args, words in ((["--status"], "--status goes with --dry-run"),
                            (["--dry-run", "--status", "--needs"], "separate dry runs")):
            with self.subTest(args=args):
                p = self.run_inbox(*args)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn(words, p.stderr)

    def test_dry_run_prints_the_rows_and_writes_nothing(self):
        self.legacy_token()
        self.profile("org_test", FakeUnio.RUNNER)
        self.fake.rows = [row(P1, IN_PROGRESS, "work_started", self.ago(3))]
        for workspace in (None, "org_test"):
            with self.subTest(workspace=workspace):
                args = ["--workspace", workspace] if workspace else []
                p = self.run_inbox(*args, "--dry-run", "--status")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("UNIO_STATUS=off", p.stdout)
                self.assertRegex(p.stdout, r"proj-p1 +planned +in_progress +work_started +ABC-1 +move")
                state = Path(inbox.Config(dict(os.environ), workspace).state_dir)
                self.assertFalse((state / "log.jsonl").exists())
        self.assertEqual(len(self.fake.seen("GET", STATUS_PATH)), 2)
        self.assertEqual(self.fake.moves(), [])


if __name__ == "__main__":
    unittest.main()
