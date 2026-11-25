"""A mention in a note on a client's page: claimed like any item, but read
with get_client, answered only in the note's thread, with no repository and
no research, and with every write on the note under the item's live claim."""
import contextlib
import io
import os
import sys
import time
from pathlib import Path
from unittest import mock

from inbox_support import FakeUnio, HomeCase, inbox

CLIENT_ID = "22222222-2222-4222-8222-222222222222"
CLAIM = "claim-0001-fake"
SCOPED = {"X-Unio-Inbox-Item": "comment:n-0002", "X-Unio-Inbox-Claim": CLAIM}


def note(content="@[Agent](user_agent) qual o status do contrato?", root="n-0001", team=True, attempts=0):
    return {
        "key": "comment:n-0002", "kind": "mention", "subject": "customer", "attempts": attempts,
        "createdAt": "2026-11-25T12:00:00.000Z", "task": None, "project": None,
        "customer": {"id": CLIENT_ID, "name": "Client Marker", "type": "PJ", "active": True,
                     "url": f"https://unio.so/app/clients/{CLIENT_ID}?tab=activity&ws=org_test",
                     "team": {"id": "t-1", "name": "Product", "identifier": "ABC"} if team else None},
        "comment": {"id": "n-0002", "rootId": root, "content": content, "createdAt": "2026-11-25T12:00:00.000Z",
                    "author": {"kind": "user", "name": "Person Marker", "clerkId": "user_person",
                               "canUpdateTasks": False, "canCreateTasks": False}},
    }


def task_item():
    return {"key": "comment:c-0001", "kind": "mention", "subject": "task", "task": {"id": "t-1", "key": "ABC-1"},
            "comment": {"id": "c-0001", "author": {"kind": "user"}}}


class Calls:
    """An Api that records each call with its headers, in order."""

    token = "unio_rt_test_runner_token"

    def __init__(self, attempts=0):
        self.attempts = attempts
        self.calls = []

    def call(self, method, path, body=None, idempotency_key=None, timeout=20, ascii_only=True, sample=True,
             headers=None):
        self.calls.append((method, path, body, headers))
        if path == "/agents/inbox/claim":
            return 200, {"data": {"key": body.get("key"), "claimId": CLAIM, "attempts": self.attempts}}
        if path in ("/agents/inbox/fail", "/agents/inbox/complete", "/agents/inbox/release"):
            return 200, {"data": {"key": body.get("key"), "attempts": self.attempts + 1}}
        if path == "/agents/me":
            return 200, {"data": {"writing": {"language": "pt-BR"},
                                  "teams": [{"id": 1, "name": "Product", "identifier": "ABC"}]}}
        if path.endswith("/notes"):
            return 201, {"data": {"id": "n-runner"}}
        return 200, {"data": {}}

    def settle(self, method, path, body, idempotency_key, timeout=20, ascii_only=True, headers=None):
        return self.call(method, path, body, idempotency_key, headers=headers)

    def seen(self):
        """(method, path, headers) of every call but progress posts."""
        return [(m, p, h) for m, p, _, h in self.calls if p != "/agents/inbox/progress"]


class Subject(HomeCase):
    def test_a_client_item_needs_its_client_and_its_comment(self):
        self.assertEqual(inbox.item_subject(note()), "customer")
        self.assertIsNone(inbox.item_subject(dict(note(), customer=None)))
        self.assertIsNone(inbox.item_subject(dict(note(), comment=None)))
        self.assertIsNone(inbox.item_subject(dict(note(), subject="invoice")))

    def test_no_research_for_a_client_note(self):
        self.profile("org_test")
        runner = inbox.Runner(inbox.Config({**os.environ, "UNIO_INBOX_WEB": "on"}, "org_test"))
        self.assertFalse(runner.research_ready(note()))
        self.assertIsNone(runner.research_ask(note("pesquise as alíquotas"), {}))
        self.assertEqual(runner.reply_web(note()), "client")

    def test_its_reply_counts_as_replied(self):
        self.assertEqual(inbox.check_outcome("replied", {"mcp__unio__reply_client_note"}), ("replied", None))
        self.assertEqual(inbox.STEPS["mcp__unio__get_client"], "reading_thread")
        self.assertEqual(inbox.STEPS["mcp__unio__reply_client_note"], "writing_reply")

    def test_research_denies_the_client_tools(self):
        for name in ("mcp__unio__get_client", "mcp__unio__reply_client_note"):
            self.assertIn(name, inbox.RESEARCH_DENIED)
        self.assertIn("mcp__unio__reply_client_note", inbox.RESEARCH_INIT_REFUSED)


class Prompt(HomeCase):
    def test_the_client_lines(self):
        text = inbox.build_prompt(note(), [], 1, teams=["Product (ABC)"], web="client", language="pt-BR")
        self.assertIn(f"Client: id {CLIENT_ID}. Read it with get_client client={CLIENT_ID}; answer with "
                      f"reply_client_note client={CLIENT_ID} reply_to=n-0002.", text)
        self.assertIn(f"Comment id: n-0002 (answer it with reply_client_note client={CLIENT_ID} reply_to=n-0002)",
                      text)
        self.assertIn("Thread root id: n-0001", text)
        self.assertIn("Team: Product, identifier ABC.\n", text)
        self.assertIn("Client: type PJ, active.", text)
        self.assertIn("writes only its reply in this note's thread and reactions on this client's notes.", text)
        self.assertIn("Repositories: none for a client note", text)
        self.assertIn(inbox.web_line("client"), text)
        self.assertIn("Web research and files: not in client notes.", text)
        for absent in ("Status:", "Author can update tasks", "Author clerk id", "Your teams in this workspace",
                       "Task title", "search_tasks"):
            self.assertNotIn(absent, text)
        untrusted = text[text.index("----- BEGIN UNTRUSTED"):text.index("----- END UNTRUSTED")]
        self.assertIn("Client name: Client Marker", untrusted)
        self.assertNotIn("Client name", text[:text.index("----- BEGIN UNTRUSTED")])

    def test_a_client_without_a_team(self):
        item = note(team=False)
        item["customer"]["active"] = False
        text = inbox.build_prompt(item, [], 2, web="client")
        self.assertIn("Team: none.\n", text)
        self.assertIn("Client: type PJ, inactive.", text)
        self.assertIn("may already have answered or made what it asked for: check the thread first.\n", text)


class Reply(HomeCase):
    def test_it_goes_to_the_notes_route_under_the_claim(self):
        self.profile("org_test")
        runner = inbox.Runner(inbox.Config(dict(os.environ), "org_test"))
        with FakeUnio() as fake:
            api = inbox.Api(fake.url, FakeUnio.RUNNER)
            self.assertIsNone(runner.reply(api, note(), {"claimId": CLAIM}, "Texto.", "unio-inbox:note:k"))
            sent, = fake.seen("POST", f"/v1/agents/customers/{CLIENT_ID}/notes")
            self.assertEqual(sent["body"], {"content": "Texto.", "parentId": "n-0001"})
            self.assertEqual(sent["headers"]["X-Unio-Inbox-Item"], "comment:n-0002")
            self.assertEqual(sent["headers"]["X-Unio-Inbox-Claim"], CLAIM)
            self.assertEqual(sent["headers"]["Idempotency-Key"], "unio-inbox:note:k")
            # A root note in the item is its own thread's root.
            runner.reply(api, note(root=None), {"claimId": CLAIM}, "Texto.", "unio-inbox:note:k2")
            self.assertEqual(fake.seen("POST", f"/v1/agents/customers/{CLIENT_ID}/notes")[-1]["body"]["parentId"],
                             "n-0002")
            # A task reply still goes out without them.
            runner.reply(api, dict(task_item(), task={"id": "t-1"}), {"claimId": CLAIM}, "T.", "k3")
            task_reply, = fake.seen("POST", "/v1/tasks/t-1/comments")
            self.assertNotIn("X-Unio-Inbox-Item", task_reply["headers"])

    def test_waiting_client_items_get_no_eyes(self):
        self.profile("org_test")
        runner = inbox.Runner(inbox.Config(dict(os.environ), "org_test"))
        api = Calls()
        runner.acknowledge(api, [note(), task_item()])
        self.assertEqual([(m, p, (b or {}).get("reactableId")) for m, p, b, _ in api.calls],
                         [("PUT", "/agents/reactions", "c-0001")])
        self.assertEqual(set(runner.acked), {"comment:c-0001"})

    def test_print_items_names_the_client_by_id(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inbox.print_items([note()])
        self.assertIn("client 22222222", out.getvalue())
        self.assertNotIn("Client Marker", out.getvalue())


class Subjects(HomeCase):
    def runner(self):
        self.profile("org_test")
        return inbox.Runner(inbox.Config(dict(os.environ), "org_test"))

    def test_a_server_before_client_notes_gets_the_legacy_subjects(self):
        runner = self.runner()
        refusal = (422, {"error": "Validation failed",
                         "messages": [{"field": "subjects", "rule": "regex", "message": "The subjects field format"}]})
        paths = []
        answers = [refusal, (200, {"data": []}), (200, {"data": []})]

        class Api:
            def call(self, method, path, body=None, idempotency_key=None, timeout=20, headers=None):
                paths.append(path)
                return answers.pop(0)

        self.assertEqual(runner.list_inbox(Api()), (200, {"data": []}))
        self.assertEqual(runner.list_inbox(Api()), (200, {"data": []}))
        self.assertEqual(paths, ["/agents/inbox?limit=10&subjects=task,project,customer",
                                 "/agents/inbox?limit=10&subjects=task,project",
                                 "/agents/inbox?limit=10&subjects=task,project"])

    def test_another_field_refused_is_answered_as_is(self):
        runner = self.runner()
        refusal = (422, {"error": "Validation failed", "messages": [{"field": "limit", "rule": "range"}]})
        paths = []

        class Api:
            def call(self, method, path, body=None, idempotency_key=None, timeout=20, headers=None):
                paths.append(path)
                return refusal

        self.assertEqual(runner.list_inbox(Api()), refusal)
        self.assertEqual(paths, ["/agents/inbox?limit=10&subjects=task,project,customer"])
        self.assertEqual(runner.subjects, inbox.SUBJECTS)


class Handle(HomeCase):
    """One client item through handle(), with the session itself faked."""

    def setUp(self):
        super().setUp()
        self.profile("org_test")
        os.environ["UNIO_INBOX_TIMEOUT"] = "1200"
        self.runner = inbox.Runner(inbox.Config(dict(os.environ), "org_test"))
        self.runner.state.ensure()
        self.runner.snapshots.for_team = mock.Mock(side_effect=AssertionError("no snapshots for a client note"))
        self.runner.presence.set = mock.Mock()
        self.sessions = []

    def handle(self, verdict, attempts=0, item=None):
        api, h = Calls(attempts), {}

        def run_session(item, prompt, snapshots, out, claim, on_wait=None, **kwargs):
            self.sessions.append({"prompt": prompt, "snapshots": snapshots, "current": dict(h.get("current") or {}),
                                  "calls": len(api.calls)})
            return inbox.Session(required=inbox.CUSTOMER_REQUIRED_TOOLS)

        with mock.patch.object(self.runner, "run_session", run_session), \
                mock.patch.object(self.runner, "classify", return_value=verdict), \
                mock.patch.object(inbox, "desk_memory", return_value=None):
            self.assertEqual(self.runner.handle(api, item or note(), h), "continue" if verdict.kind != "release"
                             else "stop")
        return api, h

    def test_an_answer(self):
        api, h = self.handle(inbox.Verdict("ok", outcome="replied", note="answered"))
        seen = api.seen()
        claim = seen.index(("POST", "/agents/inbox/claim", None))
        put = seen.index(("PUT", "/agents/reactions", SCOPED))
        delete = seen.index(("DELETE", "/agents/reactions", SCOPED))
        complete = seen.index(("POST", "/agents/inbox/complete", None))
        self.assertLess(claim, put)
        self.assertLess(put, delete)
        self.assertLess(delete, complete, "the eyes go while the claim still lives")
        self.assertEqual([c for c in seen if c[1] == "/agents/reactions"],
                         [("PUT", "/agents/reactions", SCOPED), ("DELETE", "/agents/reactions", SCOPED)],
                         "no unscoped reaction on a note")
        session, = self.sessions
        self.assertEqual(session["snapshots"], [])
        self.assertIsNone(session["current"]["target"])
        self.assertIn("reply_client_note", session["prompt"])
        self.runner.snapshots.for_team.assert_not_called()
        for call in self.runner.presence.set.call_args_list:
            self.assertNotIn("taskId", call.kwargs)
        self.assertIn(mock.call("working", itemKey="comment:n-0002"), self.runner.presence.set.call_args_list)
        self.assertNotIn("comment:n-0002", self.runner.acked)

    def test_a_failed_attempt_keeps_the_eyes(self):
        api, _ = self.handle(inbox.Verdict("fail", cause="session_error", error="boom"), attempts=0)
        seen = api.seen()
        self.assertIn(("PUT", "/agents/reactions", SCOPED), seen)
        self.assertNotIn(("DELETE", "/agents/reactions", SCOPED), seen)
        self.assertFalse(any(p.endswith("/notes") for _, p, _ in seen))
        self.assertIn(("POST", "/agents/inbox/fail", None), seen)

    def test_the_final_attempt_answers_before_the_fail(self):
        api, _ = self.handle(inbox.Verdict("fail", cause="session_error", error="boom"), attempts=2)
        seen = api.seen()
        notes = f"/agents/customers/{CLIENT_ID}/notes"
        delete = seen.index(("DELETE", "/agents/reactions", SCOPED))
        reply = seen.index(("POST", notes, SCOPED))
        fail = seen.index(("POST", "/agents/inbox/fail", None))
        self.assertLess(delete, fail)
        self.assertLess(reply, fail, "the note takes the reply only while the claim lives")
        body = next(b for m, p, b, _ in api.calls if p == notes)
        self.assertEqual(body["parentId"], "n-0001")
        self.assertIn("Não foi possível atender este pedido automaticamente", body["content"])
        self.assertEqual(len([c for c in seen if c[1] == notes]), 1)

    def test_a_long_pause_takes_the_eyes_before_the_release(self):
        until = inbox.utcnow() + inbox.timedelta(hours=1)
        verdict = inbox.Verdict("release", reason="usage_limit", error="limit", retry=until, pause=until)
        api, _ = self.handle(verdict)
        seen = api.seen()
        self.assertLess(seen.index(("DELETE", "/agents/reactions", SCOPED)),
                        seen.index(("POST", "/agents/inbox/release", None)))
        self.assertEqual([c for c in seen if c[1] == "/agents/reactions" and c[2] is None], [])

    def test_a_short_release_keeps_them(self):
        now = inbox.utcnow()
        verdict = inbox.Verdict("release", reason="memory_pressure", error="low", retry=now + inbox.MEMORY_PAUSE,
                                pause=now + inbox.MEMORY_PAUSE)
        api, _ = self.handle(verdict)
        self.assertNotIn(("DELETE", "/agents/reactions", SCOPED), api.seen())

    def test_a_timeout_note_goes_under_the_claim(self):
        api, _ = self.handle(inbox.Verdict("too_large", error="cap"))
        seen = api.seen()
        self.assertLess(seen.index(("POST", f"/agents/customers/{CLIENT_ID}/notes", SCOPED)),
                        seen.index(("POST", "/agents/inbox/complete", None)))


class SessionCap(HomeCase):
    def test_a_client_session_stops_at_the_cap_whatever_the_timeout(self):
        self.assertEqual(inbox.CUSTOMER_SESSION_CAP, 720)
        self.profile("org_test")
        # A claude that never answers: only the cap can end it.
        sleeper = self.tmp / "sleeper.py"
        sleeper.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        launcher = self.tmp / ("claude.cmd" if os.name == "nt" else "claude")
        if os.name == "nt":
            launcher.write_bytes(f'@echo off\r\n"{sys.executable}" "{sleeper}"\r\n'.encode("utf-8"))
        else:
            launcher.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{sleeper}'\n", encoding="utf-8")
            launcher.chmod(0o755)
        cfg = inbox.Config({**os.environ, "UNIO_INBOX_TIMEOUT": "1200", "UNIO_INBOX_CLAUDE": str(launcher)},
                           "org_test")
        self.assertEqual(cfg.timeout, 1200)
        runner = inbox.Runner(cfg)
        runner.state.ensure()
        out = runner.state.runs_dir / "comment_n-0002" / "attempt-1"
        began = time.monotonic()
        with mock.patch.object(inbox, "CUSTOMER_SESSION_CAP", 1):
            s = runner.run_session(note(), "prompt\n", [], out, {"claimId": CLAIM})
        self.assertTrue(s.timed_out)
        self.assertLess(time.monotonic() - began, 30)
        self.assertEqual(s.required, inbox.CUSTOMER_REQUIRED_TOOLS)
        self.assertTrue(Path(out / "prompt.txt").is_file())
