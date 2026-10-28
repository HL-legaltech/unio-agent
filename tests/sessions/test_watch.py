"""The session watcher: what it reads from Claude Code's transcripts, and when
and what it reports. The rules run in process over synthetic transcripts with a clock
the tests choose; the process tests run the watcher itself against the fake API, with a
scratch HOME, state and config, and a stand-in Claude Code process."""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from collections import defaultdict
from pathlib import Path
from unittest import mock

from support import SCRIPTS, WINDOWS, Transcript, base_env, remove_tree, usage
import fake_api
from unio_sessions import hook as session_hook
from unio_sessions import system
from unio_sessions import watch

# Waits for a process end early once their condition holds.
SLACK = float(os.environ.get("UNIO_TEST_SLACK") or 3)
T0 = 1_791_300_000.0  # 2026-10-06T15:20:00Z, on no hour boundary
HOUR = 3600


def iso(t):
    return watch.iso(t)


class Rules(unittest.TestCase):
    """The transcript rules, in process."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-watch-rules-"))
        self.addCleanup(remove_tree, self.tmp)
        self.session = str(uuid.uuid4())
        self.main_path = self.tmp / "projects/-project" / f"{self.session}.jsonl"
        self.main = Transcript(self.main_path, self.session)
        self.saved = dict(watch.TIMES)
        self.addCleanup(watch.TIMES.update, self.saved)
        watch.TIMES["list"] = 0

    def agent(self, agent_id, run=None, tool_use=None):
        folder = self.main_path.with_suffix("") / "subagents"
        if run:
            folder = folder / "workflows" / run
        if tool_use:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"agent-{agent_id}.meta.json").write_text(json.dumps({"agentType": "general-purpose",
                                                                             "toolUseId": tool_use}))
        return Transcript(folder / f"agent-{agent_id}.jsonl", self.session, sidechain=True, agent=agent_id)

    def journal(self, run, *events):
        path = self.main_path.with_suffix("") / "subagents" / "workflows" / run / "journal.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for kind, agent in events:
                f.write(json.dumps({"type": kind, "agentId": agent}) + "\n")

    def model(self, fresh=True, now=T0):
        m = watch.Model(self.main_path)
        m.adopt(fresh, now)
        return m

    @staticmethod
    def totals(m):
        out = [0, 0, 0, 0]
        for v in m.s["hours"].values():
            for i in range(4):
                out[i] += v[i]
        return out

    def test_a_call_counts_once_by_its_last_entry_in_the_hour_of_its_first(self):
        self.main.prompt(T0)
        sub = self.agent("a1", tool_use="toolu_fg")
        self.main.call(T0 + 1, tools=[("toolu_fg", "Agent", {"prompt": "x"})])
        first = HOUR * (int(T0 // HOUR) + 1) - 1  # one second before the hour turns
        sub.call(first, mid="msg_s", use=usage(2, 7, 100, 50), stop=None, text="thinking")
        sub.call(first + 2, mid="msg_s", use=usage(2, 155, 100, 50), tools=[("toolu_b", "Bash", {"command": "ls"})])
        self.main.synthetic(first + 3, "rate_limit", "You've hit your session limit")
        m = self.model()
        self.assertEqual(m.s["hours"][str(int(first // HOUR) * HOUR)], [2, 155, 100, 50])
        self.assertNotIn(str(int(first // HOUR) * HOUR + HOUR), m.s["hours"])
        self.assertEqual(self.totals(m), [2, 155, 100, 50])

    def test_an_unfinished_line_waits_until_it_is_whole(self):
        self.main.prompt(T0)
        line = json.dumps({"type": "assistant", "timestamp": iso(T0 + 1), "sessionId": self.session,
                           "message": {"id": "msg_x", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                                       "content": [], "usage": usage(1, 2, 3, 4)}})
        self.main.raw(line[:40])
        m = self.model()
        self.assertEqual(self.totals(m), [0, 0, 0, 0])
        self.main.raw(line[40:] + "\n")
        m.poll(T0 + 2)
        self.assertEqual(self.totals(m), [1, 2, 3, 4])

    def test_a_continued_sessions_copies_are_not_counted(self):
        # A continued session's file starts with its own entry, then copies of the
        # conversation it continues, stamped with their older times.
        self.main.add({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(T0)})
        self.main.prompt(T0 - 3000, "old prompt")
        self.main.call(T0 - 2990, mid="msg_old", use=usage(5, 500, 50_000, 5_000), stop="end_turn")
        self.main.prompt(T0 + 10)
        self.main.call(T0 + 20, mid="msg_new", use=usage(1, 10, 100, 10), stop="end_turn")
        m = self.model()
        self.assertEqual(self.totals(m), [1, 10, 100, 10])
        self.assertEqual(m.started_at(), T0)
        self.assertEqual(m.active.per_hour(), {int(T0 // HOUR) * HOUR: 10_000})

    def test_a_continued_session_that_appears_after_adoption_skips_its_copies(self):
        # The file appears holding the conversation it
        # continues, stamped from that one's first prompt on, then its own entries.
        m = self.model(now=T0)
        m.poll(T0 + 30)
        old = T0 - 3 * HOUR
        self.main.prompt(old, "old prompt")
        self.main.model(old, "claude-opus-5-5[1m]")
        self.main.call(old + 5, mid="msg_copied", use=usage(1, 1000, 5_000_000, 100), stop="end_turn", text="x")
        self.main.prompt(T0 + 40, "Continue")
        self.main.call(T0 + 45, mid="msg_own", use=usage(1, 7, 50, 1), stop="end_turn", text="y")
        m.poll(T0 + 50)
        self.assertEqual(self.totals(m), [1, 7, 50, 1])
        self.assertEqual(m.started_at(), T0 + 40)
        self.assertEqual(sum(m.active.per_hour().values()), 5000)
        self.assertEqual(m.context_limit(), 1_000_000, "the copied model attachment was not read")

    def test_a_turn_reads_its_steps_then_thinking_then_idle(self):
        self.main.prompt(T0)
        m = self.model()
        self.assertEqual(m.view(T0 + 5)["step"], None)
        self.assertEqual(m.view(T0 + 16), {"state": "working", "step": "thinking", "reason": None, "activeAgents": 0})
        self.main.call(T0 + 20, tools=[("t1", "Bash", {"command": "cd app && pnpm vitest run lib"})])
        m.poll(T0 + 21)
        self.assertEqual(m.view(T0 + 21)["step"], "running_tests")
        self.main.result(T0 + 40, "t1")
        m.poll(T0 + 41)
        self.assertEqual(m.view(T0 + 55)["step"], "running_tests", "a short generation gap dropped the step")
        self.assertEqual(m.view(T0 + 61)["step"], "thinking")
        self.main.call(T0 + 70, stop="end_turn", text="Done.")
        m.poll(T0 + 71)
        self.assertEqual(m.view(T0 + 100)["state"], "idle")

    def test_an_interrupt_ends_the_turn(self):
        self.main.prompt(T0)
        self.main.prompt(T0 + 5, "[Request interrupted by user]", kind=None)
        self.assertEqual(self.model().view(T0 + 60)["state"], "idle")

    def test_an_interrupted_tool_ends_the_turn(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("t1", "Bash", {"command": "sleep 100"})])
        self.main.result(T0 + 5, "t1", extra_text="[Request interrupted by user for tool use]")
        self.assertEqual(self.model().view(T0 + 60)["state"], "idle")

    def test_turn_duration_and_stop_hook_summary_end_the_turn(self):
        for subtype in ("turn_duration", "stop_hook_summary"):
            with self.subTest(subtype):
                self.main_path.unlink(missing_ok=True)
                self.main.prompt(T0)
                self.main.system(T0 + 3, subtype)
                self.assertEqual(self.model().view(T0 + 60)["state"], "idle")

    def test_what_opens_a_turn(self):
        cases = [
            ({"text": "hi", "kind": "human"}, True),
            ({"text": "Another Claude session sent a message", "kind": "peer", "meta": True}, True),
            ({"text": "<task-notification>x</task-notification>", "kind": "task-notification"}, True),
            ({"text": "Your usage limit has reset. Continue.", "kind": "auto-continuation", "meta": True}, True),
            ({"text": "Base directory for this skill: x", "kind": None, "meta": True}, False),
            ({"text": "<command-name>/effort</command-name>", "kind": None}, False),
            ({"text": "<local-command-stdout>ok</local-command-stdout>", "kind": None}, False),
            ({"text": "This session is being continued", "kind": None, "isCompactSummary": True}, False),
        ]
        for fields, opens in cases:
            with self.subTest(fields["text"][:30]):
                self.main_path.unlink(missing_ok=True)
                self.main.prompt(T0, **fields)
                self.assertEqual(self.model().view(T0 + 1)["state"], "working" if opens else "idle")

    def test_a_usage_limit_is_a_problem_until_it_resets(self):
        self.main.prompt(T0)
        self.main.synthetic(T0 + 2, "rate_limit", "You've hit your session limit · resets 9:20pm")
        self.main.system(T0 + 2.1, "informational", content="Usage limit reached · continuing automatically at 9:20pm")
        self.main.system(T0 + 2.2, "turn_duration")
        m = self.model()
        self.assertEqual(m.view(T0 + 3600), {"state": "problem", "step": None, "reason": "usage_limit",
                                             "activeAgents": 0})
        self.main.system(T0 + 7200, "informational", content="Usage limit reset · continuing automatically")
        self.main.prompt(T0 + 7200.2, "Your claude.ai usage limit has reset.", kind="auto-continuation", meta=True)
        m.poll(T0 + 7201)
        self.assertEqual(m.view(T0 + 7201)["state"], "working")
        # Blocked time is not active time.
        self.assertEqual(sum(m.active.per_hour().values()), 2000)

    def test_a_usage_limit_clears_with_the_next_real_call(self):
        self.main.prompt(T0)
        self.main.synthetic(T0 + 2, "rate_limit", "You've hit your session limit")
        m = self.model()
        self.assertEqual(m.view(T0 + 3)["reason"], "usage_limit")
        self.main.prompt(T0 + 100)
        self.main.call(T0 + 101, tools=[("t1", "Read", {"file_path": "/x"})])
        m.poll(T0 + 102)
        self.assertEqual(m.view(T0 + 102), {"state": "working", "step": "reading_code", "reason": None,
                                            "activeAgents": 0})

    def test_signed_out_is_a_problem(self):
        self.main.prompt(T0)
        self.main.synthetic(T0 + 1, "authentication_failed", "Not logged in · Please run /login")
        self.main.system(T0 + 1.1, "turn_duration")
        m = self.model()
        self.assertEqual(m.view(T0 + 600)["reason"], "signed_out")
        self.main.prompt(T0 + 700, "logged in, continue")
        self.main.call(T0 + 701, stop="end_turn", text="ok")
        m.poll(T0 + 702)
        self.assertEqual(m.view(T0 + 702)["state"], "idle")

    def test_a_limit_in_a_running_workflow_agent_is_the_sessions_problem_until_that_agent_is_done(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("toolu_wf", "Workflow", {"script": "x"})])
        self.main.result(T0 + 2, "toolu_wf", {"status": "async_launched", "taskId": "w1", "runId": "wf_1"})
        self.main.call(T0 + 3, stop="end_turn", text="Started.")
        self.main.system(T0 + 3.1, "turn_duration", pendingWorkflowCount=1)
        self.journal("wf_1", ("started", "a1"))
        a1 = self.agent("a1", run="wf_1")
        a1.prompt(T0 + 4, "task", kind=None)
        a1.synthetic(T0 + 10, "rate_limit", "You've hit your session limit")
        m = self.model()
        self.assertEqual(m.view(T0 + 600)["reason"], "usage_limit")
        self.journal("wf_1", ("result", "a1"))
        m.poll(T0 + 601)
        self.assertEqual(m.view(T0 + 601), {"state": "working", "step": "delegating", "reason": None,
                                            "activeAgents": 0})

    def test_a_background_workflow_works_until_its_notification(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("toolu_wf", "Workflow", {"script": "x"})])
        self.main.result(T0 + 2, "toolu_wf", {"status": "async_launched", "taskId": "w1", "runId": "wf_1"})
        self.main.call(T0 + 3, stop="end_turn", text="Started.")
        self.main.system(T0 + 3.1, "turn_duration", pendingWorkflowCount=1)
        self.journal("wf_1", ("started", "a1"), ("started", "a2"))
        a1, a2 = self.agent("a1", run="wf_1"), self.agent("a2", run="wf_1")
        a1.prompt(T0 + 4, "task", kind=None)
        a1.call(T0 + 5, tools=[("toolu_a1", "Bash", {"command": "git -C repo log --oneline"})])
        a2.prompt(T0 + 4, "task", kind=None)
        a2.call(T0 + 6, tools=[("toolu_a2", "Edit", {"file_path": "/x"})])
        m = self.model()
        self.assertEqual(m.view(T0 + 7), {"state": "working", "step": "editing_code", "reason": None,
                                          "activeAgents": 2})
        a2.result(T0 + 8, "toolu_a2")
        a2.call(T0 + 9, tools=[("toolu_so", "StructuredOutput", {})])
        a2.result(T0 + 9.5, "toolu_so")
        self.journal("wf_1", ("result", "a2"))
        m.poll(T0 + 10)
        self.assertEqual(m.view(T0 + 10)["step"], "using_git")
        self.assertEqual(m.view(T0 + 10)["activeAgents"], 1)
        self.main.notice(T0 + 900, "toolu_wf")
        m.poll(T0 + 901)
        self.assertEqual(m.view(T0 + 901), {"state": "idle", "step": None, "reason": None, "activeAgents": 0})

    def test_a_failed_workflow_agent_is_not_running(self):
        # 173 `failed` entries in the 195 journals of the 20 days to 2026-10-06.
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("tw", "Workflow", {})])
        self.main.result(T0 + 2, "tw", {"status": "async_launched", "taskId": "w1", "runId": "wf_1"})
        self.main.call(T0 + 3, stop="end_turn", text="Started.")
        self.journal("wf_1", ("started", "a1"), ("started", "a2"), ("failed", "a1"))
        a1 = self.agent("a1", run="wf_1")
        a1.prompt(T0 + 4, "task", kind=None)
        a1.synthetic(T0 + 5, "rate_limit", "You've hit your session limit")
        self.agent("a2", run="wf_1").prompt(T0 + 4, "task", kind=None)
        self.assertEqual(self.model().view(T0 + 60), {"state": "working", "step": "delegating", "reason": None,
                                                      "activeAgents": 1})

    def test_an_agent_silent_for_half_an_hour_is_not_running(self):
        # A workflow keeps its `started` lines and gets no result once its
        # conversation continues in another session.
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("tw", "Workflow", {})])
        self.main.result(T0 + 2, "tw", {"status": "async_launched", "taskId": "w1", "runId": "wf_1"})
        self.main.call(T0 + 3, stop="end_turn", text="Started.")
        self.main.system(T0 + 3.1, "turn_duration")
        self.journal("wf_1", ("started", "a1"), ("started", "a2"))
        self.agent("a1", run="wf_1").prompt(T0 + 4, "task", kind=None)
        a2 = self.agent("a2", run="wf_1")
        a2.prompt(T0 + 4, "task", kind=None)
        a2.call(T0 + 1500, tools=[("ta2", "Bash", {"command": "pnpm vitest run"})])
        m = self.model()
        self.assertEqual(m.view(T0 + 1700)["activeAgents"], 2)
        self.assertEqual(m.view(T0 + 1900), {"state": "working", "step": "running_tests", "reason": None,
                                             "activeAgents": 1})
        self.assertEqual(m.view(T0 + 3400), {"state": "idle", "step": None, "reason": None, "activeAgents": 0})

    def test_a_workflow_whose_agents_never_wrote_stops_reading_as_work(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("tw", "Workflow", {})])
        self.main.result(T0 + 2, "tw", {"status": "async_launched", "taskId": "w1", "runId": "wf_1"})
        self.main.call(T0 + 3, stop="end_turn", text="Started.")
        self.main.system(T0 + 3.1, "turn_duration")
        m = self.model()
        self.assertEqual(m.view(T0 + 600)["state"], "working")
        self.assertEqual(m.view(T0 + 1900)["state"], "idle")

    def test_a_conversation_continued_elsewhere_drops_what_it_had_in_flight(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("tw", "Workflow", {})])
        self.main.result(T0 + 2, "tw", {"status": "async_launched", "taskId": "w1", "runId": "wf_1"})
        self.journal("wf_1", ("started", "a1"))
        self.agent("a1", run="wf_1").prompt(T0 + 4, "task", kind=None)
        self.main.add({"type": "continued-in", "continuedInSessionId": str(uuid.uuid4()), "timestamp": iso(T0 + 10)})
        m = self.model()
        self.assertEqual(m.view(T0 + 20), {"state": "idle", "step": None, "reason": None, "activeAgents": 0})
        self.assertEqual(m.s["continuedAt"], T0 + 10)

    def test_a_background_agent_works_until_its_notification(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("toolu_ag", "Agent", {"prompt": "x", "run_in_background": True})])
        self.main.result(T0 + 2, "toolu_ag", {"isAsync": True, "status": "async_launched", "agentId": "a9"})
        self.main.call(T0 + 3, stop="end_turn", text="Started.")
        self.main.system(T0 + 3.1, "turn_duration")
        self.agent("a9", tool_use="toolu_ag").prompt(T0 + 4, "task", kind=None)
        m = self.model()
        self.assertEqual(m.view(T0 + 300), {"state": "working", "step": "delegating", "reason": None,
                                            "activeAgents": 1})
        self.main.notice(T0 + 400, "toolu_ag", via="attachment")
        m.poll(T0 + 401)
        self.assertEqual(m.view(T0 + 401)["state"], "idle")

    def test_task_stop_ends_a_background_task(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("toolu_bg", "Bash", {"command": "pnpm test", "run_in_background": True})])
        self.main.result(T0 + 2, "toolu_bg", {"backgroundTaskId": "b77", "stdout": ""})
        self.main.call(T0 + 3, stop="end_turn", text="Running.")
        self.main.system(T0 + 3.1, "turn_duration")
        m = self.model()
        self.assertEqual(m.view(T0 + 60), {"state": "working", "step": "running_tests", "reason": None,
                                           "activeAgents": 0})
        self.main.prompt(T0 + 70)
        self.main.call(T0 + 71, tools=[("toolu_stop", "TaskStop", {"task_id": "b77"})])
        self.main.result(T0 + 72, "toolu_stop", {"message": "Successfully stopped task: b77", "task_id": "b77"})
        self.main.call(T0 + 73, stop="end_turn", text="Stopped.")
        m.poll(T0 + 74)
        self.assertEqual(m.view(T0 + 74)["state"], "idle")

    def test_a_forgotten_background_command_stops_reading_as_work(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("toolu_bg", "Bash", {"command": "pnpm dev", "run_in_background": True})])
        self.main.result(T0 + 2, "toolu_bg", {"backgroundTaskId": "b1"})
        self.main.call(T0 + 3, stop="end_turn", text="Serving.")
        m = self.model()
        self.assertEqual(m.view(T0 + 600)["state"], "working")
        self.assertEqual(m.view(T0 + 2 + 1800)["state"], "idle")

    def test_a_question_waits_for_a_person_and_its_wait_is_not_active_time(self):
        self.main.prompt(T0)
        self.main.call(T0 + 10, tools=[("toolu_q", "AskUserQuestion", {"questions": []})])
        m = self.model()
        self.assertEqual(m.view(T0 + 100), {"state": "waiting", "step": "waiting_for_person", "reason": None,
                                            "activeAgents": 0})
        self.main.result(T0 + 250, "toolu_q")
        self.main.call(T0 + 260, stop="end_turn", text="ok")
        m.poll(T0 + 261)
        self.assertEqual(m.view(T0 + 261)["state"], "idle")
        self.assertEqual(sum(m.active.per_hour().values()), 20_000)

    def test_active_time_is_the_union_of_short_gaps_and_running_tools(self):
        self.main.prompt(T0)
        self.main.call(T0 + 60, tools=[("t1", "Bash", {"command": "pnpm test"})])
        self.main.result(T0 + 60 + 700, "t1")       # a 700 s tool run counts whole
        self.main.call(T0 + 770, stop="end_turn", text="done")
        self.main.prompt(T0 + 2000)                 # 20 min later: the pause does not count
        self.main.call(T0 + 2030, stop="end_turn", text="hi")
        sub = self.agent("a1", tool_use="t_none")
        sub.prompt(T0 + 100, "x", kind=None)        # overlaps the main chain's tool run
        sub.call(T0 + 200, stop="end_turn", text="x")
        m = self.model()
        self.assertEqual(sum(m.active.per_hour().values()), (770 + 30) * 1000)

    def test_a_tool_still_running_is_active_time_up_to_now(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("tb", "Bash", {"command": "pnpm vitest run"})])
        m = self.model()
        hour = watch.hour_of(T0)
        self.assertEqual(m.buckets()[hour][4], 1000)
        self.assertEqual(m.buckets(T0 + 601)[hour][4], 601_000)
        self.assertEqual(m.activity_end(T0 + 601), T0 + 601)
        self.main.result(T0 + 900, "tb", {"stdout": "ok"})
        m.poll(T0 + 901)
        self.assertEqual(m.buckets(T0 + 2000)[hour][4], 900_000)
        self.assertEqual(m.activity_end(T0 + 2000), T0 + 900)

    def test_active_time_splits_at_the_hour(self):
        edge = HOUR * (int(T0 // HOUR) + 1)
        self.main.prompt(edge - 100)
        self.main.call(edge + 50, stop="end_turn", text="x")
        m = self.model()
        self.assertEqual(m.active.per_hour(), {edge - HOUR: 100_000, edge: 50_000})

    def test_active_time_read_late_from_another_file_is_not_counted_twice(self):
        # The main file is read first, hours past the work, then each agent's: the same
        # 20 minutes in two files are 20 minutes.
        self.main.prompt(T0)
        self.main.call(T0 + 60, tools=[("tw", "Workflow", {})])
        self.main.result(T0 + 61, "tw", {"status": "async_launched", "taskId": "w1", "runId": "wf_1"})
        for k in range(1, 21):
            self.main.call(T0 + 61 + k * 60, tools=[(f"r{k}", "Read", {})])
            self.main.result(T0 + 62 + k * 60, f"r{k}")
        self.main.call(T0 + 1300, stop="end_turn", text="x")
        late = T0 + 8 * HOUR
        self.main.prompt(late)
        self.main.call(late + 30, stop="end_turn", text="y")
        self.journal("wf_1", ("started", "a1"))
        a = self.agent("a1", run="wf_1")
        a.prompt(T0 + 62, "task", kind=None)
        for k in range(1, 21):
            a.call(T0 + 62 + k * 60, tools=[(f"b{k}", "Read", {})])
            a.result(T0 + 63 + k * 60, f"b{k}")
        self.assertEqual(sum(self.model().active.per_hour().values()), (1300 + 30) * 1000)

    def test_the_context_limit_comes_from_the_model_attachment(self):
        self.main.model(T0, "claude-opus-5-5[1m]")
        self.main.prompt(T0)
        self.main.call(T0 + 1, use=usage(1, 10, 400_000, 12_000), stop="end_turn", text="x")
        m = self.model()
        self.assertEqual((m.s["context"], m.context_limit(), m.model()), (412_001, 1_000_000, "claude-opus-5-5[1m]"))

    def test_a_model_attachment_stamped_just_before_the_prompt_still_counts(self):
        # 30 of 128 recent heads: the prompt is the first stamped entry, its attachments 4 ms earlier.
        self.main.add({"type": "file-history-snapshot"})
        self.main.prompt(T0 + 0.004)
        self.main.model(T0, "claude-opus-5-5[1m]")
        self.main.call(T0 + 5, use=usage(1, 10, 80_000, 7_000), stop="end_turn", text="x")
        m = self.model()
        self.assertEqual((m.model(), m.context_limit()), ("claude-opus-5-5[1m]", 1_000_000))

    def test_a_session_joined_at_its_end_reads_its_model_from_the_head(self):
        self.main.prompt(T0)
        self.main.model(T0, "claude-opus-5-5[1m]")
        filler = "x" * 5000
        for n in range(500):  # 2.5 MB: the attachment is out of the tail
            self.main.call(T0 + 1 + n, use=usage(1, 1, 10_000, 0), stop="end_turn", text=filler)
        m = self.model(fresh=False, now=T0 + 600)
        self.assertEqual((m.model(), m.context_limit()), ("claude-opus-5-5[1m]", 1_000_000))

    def test_without_the_attachment_a_context_past_200k_still_means_1m(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, use=usage(1, 10, 100_000, 0), stop="end_turn", text="x")
        m = self.model()
        self.assertEqual((m.context_limit(), m.model()), (200_000, "claude-opus-5-5"))
        self.main.call(T0 + 2, use=usage(1, 10, 300_000, 0), stop="end_turn", text="x")
        m.poll(T0 + 3)
        self.assertEqual(m.context_limit(), 1_000_000)

    def test_the_context_is_the_main_chains_latest_call(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, use=usage(1, 10, 50_000, 2_000), tools=[("t1", "Agent", {})])
        self.agent("a1", tool_use="t1").call(T0 + 2, use=usage(1, 10, 900_000, 0), stop="end_turn", text="x")
        self.assertEqual(self.model().s["context"], 52_001)

    def test_the_title_is_claude_codes_title(self):
        self.main.add({"type": "ai-title", "aiTitle": "Status page layout"})
        self.main.prompt(T0)
        self.assertEqual(self.model().s["title"], "Status page layout")

    def test_a_running_session_is_joined_at_its_end(self):
        self.main.prompt(T0)
        for n in range(200):
            self.main.call(T0 + 1 + n, use=usage(1, 100, 10_000, 100), tools=[(f"t{n}", "Read", {})])
            self.main.result(T0 + 1.5 + n, f"t{n}")
        old = self.agent("a_old", tool_use="t_old")
        old.call(T0 + 5, use=usage(1, 1, 1, 1), stop="end_turn", text="x")
        self.main.call(T0 + 300, tools=[("toolu_q", "AskUserQuestion", {})])
        now = T0 + 400
        m = self.model(fresh=False, now=now)
        self.assertEqual(self.totals(m), [0, 0, 0, 0], "history was counted")
        self.assertEqual((m.s["countedSince"], m.started_at()), (now, T0))
        self.assertEqual(m.view(now)["state"], "waiting", "the tail did not rebuild the state")
        self.main.result(now + 10, "toolu_q")
        self.main.call(now + 20, use=usage(2, 20, 2_000, 20), stop="end_turn", text="ok")
        old.call(now + 21, use=usage(3, 3, 3, 3), stop="end_turn", text="y")
        new = self.agent("a_new", tool_use="t_new")
        new.call(now + 22, use=usage(4, 4, 4, 4), stop="end_turn", text="z")
        m.poll(now + 30)
        self.assertEqual(self.totals(m), [9, 27, 2007, 27])
        # Only the 10 s from the answer to the next call: the wait itself and the old
        # agent's 416 s gap do not count.
        self.assertEqual(sum(m.active.per_hour().values()), 10_000, "time before countedSince was counted")

    def test_a_replaced_or_cut_transcript_is_joined_again_without_counting_twice(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, mid="msg_a", use=usage(1, 10, 100, 1000), stop="end_turn", text="x")
        m = self.model()
        data = self.main_path.read_bytes()
        replacement = self.main_path.with_suffix(".new")
        replacement.write_bytes(data)
        replacement.replace(self.main_path)
        m.poll(T0 + 2)
        self.assertEqual(self.totals(m), [1, 10, 100, 1000], "a new inode was read again")
        self.main.call(T0 + 3, mid="msg_b", use=usage(2, 20, 200, 2000), stop="end_turn", text="y")
        m.poll(T0 + 4)
        self.assertEqual(self.totals(m), [3, 30, 300, 3000])
        self.main_path.write_bytes(b"")
        m.poll(T0 + 5)
        self.main.call(T0 + 6, mid="msg_c", use=usage(4, 40, 400, 4000), stop="end_turn", text="z")
        m.poll(T0 + 7)
        self.assertEqual(self.totals(m), [7, 70, 700, 7000], "what was written after the cut was lost")

    def test_an_agent_file_cold_at_adoption_reads_its_head_once_it_grows(self):
        self.main.prompt(T0)
        old = self.agent("a_old", tool_use="t_old")
        old.prompt(T0 + 1, "task", kind=None)
        os.utime(old.path, (T0 - HOUR, T0 - HOUR))
        m = self.model(fresh=False, now=T0 + 10)
        f = m.file("subagents/agent-a_old.jsonl")
        self.assertEqual((f["firstTs"], f.get("headPending")), (None, True))
        old.call(T0 + 20, use=usage(1, 1, 1, 1), stop="end_turn", text="x")
        m.poll(T0 + 21)
        self.assertEqual((f["firstTs"], f.get("headPending")), (T0 + 1, None))
        self.assertEqual(self.totals(m), [1, 1, 1, 1])

    def test_a_new_process_forgets_what_the_old_one_had_in_flight(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, tools=[("toolu_bg", "Bash", {"command": "pnpm dev", "run_in_background": True})])
        self.main.result(T0 + 2, "toolu_bg", {"backgroundTaskId": "b1"})
        self.main.call(T0 + 3, tools=[("t1", "Bash", {"command": "pnpm test"})])
        m = self.model()
        self.assertEqual(m.view(T0 + 4)["step"], "running_tests")
        m.new_process()
        self.assertEqual(m.view(T0 + 5), {"state": "idle", "step": None, "reason": None, "activeAgents": 0})

    def test_shell_commands_are_named_locally(self):
        cases = {
            "cd backend && TZ=UTC node ace test functional --files=x": "running_tests",
            "pnpm vitest run lib/format-tokens.test.ts": "running_tests",
            "python3 -m unittest test_session_watch": "running_tests",
            "timeout 300 python3 tests/test_session_hook.py": "running_tests",
            "cargo test": "running_tests",
            "pnpm typecheck": "running_checks",
            "pnpm exec tsc --noEmit": "running_checks",
            "npx eslint . && git status": "running_checks",
            "pnpm build": "running_checks",
            "git -C repo log --oneline -3": "using_git",
            "cd x && git commit -m 'feat: y'": "using_git",
            "gh pr view 12": "using_git",
            "ls -la /tmp": "running_command",
            "sleep 30; curl -s localhost": "running_command",
            "": "running_command",
        }
        for command, step in cases.items():
            with self.subTest(command):
                self.assertEqual(watch.bash_step(command), step)


def busy_session(main, agent_of, journal, start):
    """A synthetic session of a few hundred calls across an hour boundary: the
    main chain, a foreground agent, a background agent on another model, partly
    in fast mode, and a workflow's two agents writing to the 1 h cache, with
    streamed calls that repeat a message id as their output and thinking grow,
    a call on a model no Claude id names, a long prompt that searched the web,
    and usage-limit entries that must never count."""
    main.prompt(start, "build it")
    sub = agent_of("fg1", tool_use="toolu_fg")
    bg = agent_of("bg1", tool_use="toolu_bg")
    main.call(start + 1, tools=[("toolu_fg", "Agent", {"prompt": "x"})], use=usage(5, 50, 1000, 100))
    for n in range(60):
        t = start + 2 + n * 30
        sub.call(t, mid=f"msg_fg_{n}", use=usage(1, n, 2000 + n, 10, output_tokens_details={"thinking_tokens": n // 2}),
                 stop=None, text="partial")
        sub.call(t + 1, mid=f"msg_fg_{n}", use=usage(1, 3 * n + 7, 2000 + n, 10,
                                                     output_tokens_details={"thinking_tokens": n}),
                 tools=[(f"fg_t{n}", "Read", {"file_path": "/x"})])
        sub.result(t + 2, f"fg_t{n}")
    main.result(start + 1900, "toolu_fg")
    main.call(start + 1901, tools=[("toolu_bg", "Agent", {"prompt": "y", "run_in_background": True})],
              use=usage(2, 20, 3000, 30))
    main.result(start + 1902, "toolu_bg", {"isAsync": True, "status": "async_launched", "agentId": "bg1"})
    main.call(start + 1903, tools=[("toolu_wf", "Workflow", {"script": "z"})], use=usage(3, 30, 4000, 40))
    main.result(start + 1904, "toolu_wf", {"status": "async_launched", "taskId": "w1", "runId": "wf_1"})
    journal("wf_1", ("started", "w_a"), ("started", "w_b"))
    for n in range(80):
        t = start + 1905 + n * 40
        bg.call(t, mid=f"msg_bg_{n}", use=usage(2, n + 1, 5000, 0, speed="fast" if n % 3 else "standard"),
                tools=[(f"bg_t{n}", "Bash", {"command": "ls"})], model="claude-haiku-5-5")
        bg.result(t + 1, f"bg_t{n}")
    for name in ("w_a", "w_b"):
        w = agent_of(name, run="wf_1")
        w.prompt(start + 1906, "task", kind=None)
        for n in range(40):
            t = start + 1907 + n * 50
            w.call(t, mid=f"msg_{name}_{n}", tools=[(f"{name}_t{n}", "Grep", {})], use=usage(
                1, 2, 300, 4, cache_creation={"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 3}))
            w.result(t + 1, f"{name}_t{n}")
        w.synthetic(start + 3990, "rate_limit", "You've hit your session limit")
    journal("wf_1", ("result", "w_a"), ("result", "w_b"))
    main.synthetic(start + 4000, "rate_limit", "You've hit your session limit")
    main.call(start + 4050, use=usage(9, 90, 900, 9), stop=None, text="x", model="claude-Opus-5-5")
    main.call(start + 4100, use=usage(4, 40, 120_000, 60, server_tool_use={"web_search_requests": 2,
                                                                            "web_fetch_requests": 1}),
              stop="end_turn", text="done")
    main.system(start + 4100.1, "turn_duration")


def independent_count(files):
    """Last entry per message.id, <synthetic> excluded, in the hour of the id's first entry."""
    hours = defaultdict(lambda: [0, 0, 0, 0])
    for path in files:
        last, first = {}, {}
        with open(path, "rb") as fh:
            lines = fh.readlines()
        for line in lines:
            e = json.loads(line)
            m = e.get("message") or {}
            if e.get("type") != "assistant" or m.get("model") == "<synthetic>" or not isinstance(m.get("usage"), dict):
                continue
            key = m.get("id") or e.get("uuid")
            first.setdefault(key, watch.stamp(e["timestamp"]))
            u = m["usage"]
            last[key] = [u.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens")]
        for key, v in last.items():
            h = int(first[key] // HOUR) * HOUR
            for i in range(4):
                hours[h][i] += v[i]
    return dict(hours)


def independent_parts(main_path, files):
    """Per hour, the parts the sessions route takes: per model, agent, speed and
    long prompt, decided by each message.id's first entry, its last entry's
    numbers."""
    hours = defaultdict(lambda: defaultdict(lambda: [0] * 9))
    for path in files:
        first, last = {}, {}
        with open(path, "rb") as fh:
            lines = fh.readlines()
        for line in lines:
            e = json.loads(line)
            m = e.get("message") or {}
            if e.get("type") != "assistant" or m.get("model") == "<synthetic>" or not isinstance(m.get("usage"), dict):
                continue
            key = m.get("id") or e.get("uuid")
            u = m["usage"]
            prompt = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens"))
            first.setdefault(key, (watch.stamp(e["timestamp"]), m.get("model"),
                                   "fast" if u.get("speed") == "fast" else "standard", prompt > 100_000))
            last[key] = u
        for key, u in last.items():
            t, model, speed, long_prompt = first[key]
            if not isinstance(model, str) or not re.fullmatch(r"claude-[a-z0-9.-]{1,48}(\[1m\])?", model):
                continue
            written = u.get("cache_creation_input_tokens") or 0
            hour_cache = min((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0, written)
            tools = u.get("server_tool_use") or {}
            agent = "main" if path == main_path else "subagent"
            v = hours[int(t // HOUR) * HOUR][watch.part_key(model, agent, speed, long_prompt)]
            thinking = (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
            numbers = (1, u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                       u.get("cache_read_input_tokens") or 0, written - hour_cache, hour_cache, thinking,
                       tools.get("web_search_requests") or 0, tools.get("web_fetch_requests") or 0)
            for i, x in enumerate(numbers):
                v[i] += x
    return {h: dict(p) for h, p in hours.items()}


class IndependentCount(unittest.TestCase):
    """A busy synthetic session against a count that shares no code with the watcher."""

    setUp, agent, journal, model = Rules.setUp, Rules.agent, Rules.journal, Rules.model

    def files(self):
        return [self.main_path, *sorted(self.main_path.with_suffix("").rglob("agent-*.jsonl"))]

    def test_the_watchers_hours_equal_an_independent_count(self):
        busy_session(self.main, self.agent, self.journal, T0)
        m = self.model(now=T0 + 5000)
        hours = independent_count(self.files())
        self.assertGreater(len(hours), 1, "the session should cross an hour")
        self.assertEqual({int(h): v for h, v in m.s["hours"].items()}, hours)

    def test_the_watchers_parts_equal_an_independent_count(self):
        busy_session(self.main, self.agent, self.journal, T0)
        m = self.model(now=T0 + 5000)
        parts = independent_parts(self.main_path, self.files())
        self.assertEqual(len({k for p in parts.values() for k in p}), 5, "the session should mix its parts")
        self.assertEqual(m.parts(), parts)
        for h, p in parts.items():
            summed = [sum(v[i] for v in p.values()) for i in range(1, 6)]
            totals = m.s["hours"][str(h)]
            self.assertTrue(all(a <= b for a, b in zip(summed[:3] + [summed[3] + summed[4]], totals)),
                            f"{summed} past {totals}")

    def test_stamps_parse_as_python_39_reads_none_of_them(self):
        self.assertEqual(watch.stamp("2026-10-06T15:20:00.000Z"), T0)
        self.assertEqual(watch.stamp("2026-10-06T15:20:00Z"), T0)
        self.assertEqual(watch.stamp("2026-10-06T12:20:00.5-03:00"), T0 + 0.5)
        self.assertAlmostEqual(watch.stamp("2026-10-06T15:20:00.123456789+00:00"), T0 + 0.123456789, places=6)
        for bad in ("", "yesterday", "2026-13-06T15:20:00Z", None, 5):
            self.assertIsNone(watch.stamp(bad), bad)


class Parts(unittest.TestCase):
    """Each hour's calls split by model, agent, speed and long prompt, in process.
    A part reads [calls, input, output, cache read, cache write 5 min, cache
    write 1 h, thinking, web searches, web fetches]."""

    setUp, agent, model, totals = Rules.setUp, Rules.agent, Rules.model, staticmethod(Rules.totals)
    H = int(T0 // HOUR) * HOUR

    @staticmethod
    def key(model="claude-opus-5-5", agent="main", speed="standard", long_prompt=False):
        return watch.part_key(model, agent, speed, long_prompt)

    def test_a_part_per_model_agent_speed_and_long_prompt(self):
        self.main.prompt(T0)
        sub = self.agent("a1", tool_use="toolu_fg")
        self.main.call(T0 + 1, mid="m1", use=usage(1, 10, 100, 0), tools=[("toolu_fg", "Agent", {"prompt": "x"})])
        sub.call(T0 + 2, mid="s1", use=usage(2, 20, 200, 0), model="claude-sonnet-5-5", stop="end_turn", text="x")
        self.main.result(T0 + 3, "toolu_fg")
        self.main.call(T0 + 4, mid="m2", use=usage(3, 30, 300, 0, speed="fast"), stop=None, text="x")
        self.main.call(T0 + 5, mid="m3", use=usage(4, 40, 99_000, 996, speed="standard"), stop=None, text="x")
        self.main.call(T0 + 6, mid="m4", use=usage(5, 50, 99_000, 996), stop="end_turn", text="x")
        m = self.model()
        self.assertEqual(m.parts(), {self.H: {
            self.key(): [2, 5, 50, 99_100, 996, 0, 0, 0, 0],  # m3's prompt is 100,000 tokens: not over
            self.key("claude-sonnet-5-5", "subagent"): [1, 2, 20, 200, 0, 0, 0, 0, 0],
            self.key(speed="fast"): [1, 3, 30, 300, 0, 0, 0, 0, 0],
            self.key(long_prompt=True): [1, 5, 50, 99_000, 996, 0, 0, 0, 0],
        }})

    def test_cache_writes_split_by_lifetime_and_what_the_split_leaves_out_is_5_min(self):
        self.main.prompt(T0)
        splits = [None,  # no split: the API's default lifetime
                  {"ephemeral_5m_input_tokens": 400, "ephemeral_1h_input_tokens": 600},
                  {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 300},
                  {"ephemeral_1h_input_tokens": 1500},
                  {"ephemeral_5m_input_tokens": "x", "ephemeral_1h_input_tokens": None}]
        for n, split in enumerate(splits):
            more = {} if split is None else {"cache_creation": split}
            self.main.call(T0 + n * HOUR, mid=f"m{n}", use=usage(0, 1, 0, 1000, **more), stop="end_turn", text="x")
            self.main.prompt(T0 + n * HOUR + 1)
        m = self.model(now=T0 + 5 * HOUR)
        self.assertEqual([m.parts()[self.H + n * HOUR][self.key()][4:6] for n in range(len(splits))],
                         [[1000, 0], [400, 600], [700, 300], [0, 1000], [1000, 0]])

    def test_thinking_web_searches_and_fetches(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, mid="m1", stop=None, text="x", use=usage(
            2, 711, 29_356, 37_860, output_tokens_details={"thinking_tokens": 281},
            server_tool_use={"web_search_requests": 2, "web_fetch_requests": 3}, service_tier="standard",
            cache_creation={"ephemeral_1h_input_tokens": 37_860, "ephemeral_5m_input_tokens": 0},
            inference_geo="global", speed="standard"))
        self.main.call(T0 + 2, mid="m2", use=usage(1, 1, 1, 0, server_tool_use=None), stop="end_turn", text="x")
        m = self.model()
        self.assertEqual(m.parts()[self.H], {self.key(): [2, 3, 712, 29_357, 0, 37_860, 281, 2, 3]})

    def test_a_repeated_subagent_message_counts_once_with_its_growth(self):
        self.main.prompt(T0)
        sub = self.agent("a1", tool_use="toolu_fg")
        self.main.call(T0 + 1, mid="m1", tools=[("toolu_fg", "Agent", {"prompt": "x"})])
        first = self.H + HOUR - 1  # one second before the hour turns
        sub.call(first, mid="msg_s", use=usage(2, 7, 100, 50, output_tokens_details={"thinking_tokens": 5}),
                 stop=None, text="thinking")
        # Past 100,000 prompt tokens and in fast mode only from its second entry: the first decides.
        sub.call(first + 2, mid="msg_s", tools=[("toolu_b", "Bash", {"command": "ls"})], use=usage(
            2, 155, 100_000, 50, speed="fast", output_tokens_details={"thinking_tokens": 90},
            server_tool_use={"web_search_requests": 1, "web_fetch_requests": 0}))
        sub.call(first + 3, mid="msg_s", tools=[("toolu_b", "Bash", {"command": "ls"})], use=usage(
            2, 160, 100_000, 50, speed="fast", output_tokens_details={"thinking_tokens": 90},
            server_tool_use={"web_search_requests": 1, "web_fetch_requests": 0}))
        m = self.model()
        self.assertEqual(m.parts(), {self.H: {self.key(agent="subagent"): [1, 2, 160, 100_000, 50, 0, 90, 1, 0],
                                              self.key(): [1, 0, 0, 0, 0, 0, 0, 0, 0]}})

    def test_a_call_is_one_call_however_many_entries_carry_it(self):
        self.main.prompt(T0)
        for n, out in enumerate((3, 9, 20)):  # a thinking block, a text block, a tool call
            self.main.call(T0 + 1 + n, mid="msg_a", use=usage(1, out, 100, 10), stop=None, text="x")
        self.main.call(T0 + 5, mid="msg_b", use=usage(1, 4, 100, 0), stop="end_turn", text="y")
        m = self.model()
        self.assertEqual(m.parts()[self.H], {self.key(): [2, 2, 24, 200, 10, 0, 0, 0, 0]})

    def test_a_model_no_claude_id_names_counts_in_the_totals_only(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, mid="a", use=usage(1, 10, 100, 1000), stop=None, text="x")
        self.main.call(T0 + 2, mid="b", use=usage(2, 20, 200, 2000), stop=None, text="x", model="gpt-5")
        self.main.call(T0 + 3, mid="c", use=usage(4, 40, 400, 4000), stop=None, text="x", model="Claude-Opus-5-5")
        self.main.call(T0 + 4, mid="d", use=usage(8, 80, 800, 8000), stop="end_turn", text="x", model=None)
        m = self.model()
        self.assertEqual(self.totals(m), [15, 150, 1500, 15000])
        self.assertEqual(m.parts(), {self.H: {self.key(): [1, 1, 10, 100, 1000, 0, 0, 0, 0]}})

    def test_parts_count_only_what_the_totals_count(self):
        # A running session joined at its end counts only what grows from then on.
        self.main.prompt(T0)
        self.main.call(T0 + 1, mid="msg_a", use=usage(1, 5, 100, 10), stop=None, text="x")
        m = self.model(fresh=False, now=T0 + 2)
        self.main.call(T0 + 3, mid="msg_a", use=usage(1, 9, 100, 10), stop=None, text="x")
        self.main.call(T0 + 4, mid="msg_b", use=usage(2, 20, 200, 20), stop="end_turn", text="y")
        m.poll(T0 + 5)
        self.assertEqual(self.totals(m), [2, 24, 200, 20])
        self.assertEqual(m.parts(), {self.H: {self.key(): [1, 2, 24, 200, 20, 0, 0, 0, 0]}})

    def test_state_saved_before_parts_loads_and_counts_parts_from_then_on(self):
        self.main.prompt(T0)
        self.main.call(T0 + 1, mid="msg_a", use=usage(1, 10, 100, 0), stop=None, text="x")
        state = json.loads(json.dumps(self.model().dump()))
        del state["parts"]
        for f in state["files"].values():
            del f["lastMessagePart"], f["lastMessageDetail"]
        m = watch.Model(self.main_path, state, T0 + 2)
        self.main.call(T0 + 3, mid="msg_a", use=usage(1, 30, 100, 0), stop=None, text="x")
        self.main.call(T0 + 4, mid="msg_b", use=usage(2, 20, 200, 0), stop="end_turn", text="y")
        m.poll(T0 + 5)
        self.assertEqual(self.totals(m), [3, 50, 300, 0])
        self.assertEqual(m.parts(), {self.H: {self.key(): [1, 2, 20, 200, 0, 0, 0, 0, 0]}},
                         "a call begun before the parts existed went into one")


class ReporterCase(unittest.TestCase):
    """The cadence, with injected senders and a clock the tests move."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-watch-report-"))
        self.addCleanup(remove_tree, self.tmp)
        self.session = str(uuid.uuid4())
        self.main = Transcript(self.tmp / f"{self.session}.jsonl", self.session)
        self.main.prompt(T0)
        self.main.call(T0 + 1, use=usage(1, 2, 3, 4), tools=[("t1", "Bash", {"command": "pnpm test"})])
        self.m = watch.Model(self.main.path)
        self.m.adopt(True, T0 + 2)
        self.m.s["watchingSince"] = T0 + 2
        self.sent, self.quiet, self.puts = [], [], []
        self.answers = []  # (status, payload) per put, else 200
        self.r = watch.Reporter(self.m, self.session, self.presence, lambda: self.quiet.append(True), self.put, "laptop")

    def presence(self, want):
        self.sent.append(want)
        return 200

    def put(self, session_id, body):
        self.assertEqual(session_id, watch.presence_id(self.session))
        self.puts.append(body)
        return self.answers.pop(0) if self.answers else (200, {"data": {"id": session_id, "stored": True}})

    def tick(self, now, task=None):
        view = self.m.view(now)
        self.r.presence(now, view, task)
        self.r.session(now, view, task)
        return view


class Cadence(ReporterCase):
    def test_new_steps_wait_for_the_sessions_route(self):
        self.tick(T0 + 2)
        self.assertEqual(self.sent, [{"state": "working", "taskId": None}])
        self.assertEqual(len(self.puts), 1)
        self.tick(T0 + 14)
        self.assertEqual(self.sent[-1], {"state": "working", "taskId": None, "step": "running_tests"})

    def test_a_route_the_server_lacks_keeps_old_presence_and_backs_off_an_hour(self):
        self.answers = [(404, {"error": "no route"})]
        self.tick(T0 + 2)
        for t in range(3, 3000, 7):
            self.tick(T0 + t)
        self.assertEqual(len(self.puts), 1, "the route was retried within the hour")
        self.assertTrue(all("step" not in w for w in self.sent), self.sent)
        self.tick(T0 + 2 + 3600)
        self.assertEqual(len(self.puts), 2)

    def test_a_429_backs_off_five_minutes(self):
        self.answers = [(429, {"code": "agent_session_limit"})]
        self.tick(T0 + 2)
        self.tick(T0 + 200)
        self.assertEqual(len(self.puts), 1)
        self.tick(T0 + 303)
        self.assertEqual(len(self.puts), 2)

    def test_a_forbidden_field_is_dropped_and_the_report_sent_again(self):
        self.m.s["title"] = "Status page"
        self.answers = [(422, {"code": "agent_field_forbidden", "field": "title"})]
        self.tick(T0 + 2)
        self.tick(T0 + 4)
        self.assertEqual(["title" in b for b in self.puts], [True, False])

    def test_a_task_outside_the_agents_teams_is_left_out(self):
        task = str(uuid.uuid4())
        self.answers = [(422, {"code": "agent_task_forbidden", "field": "taskId"})]
        self.tick(T0 + 2, task)
        self.tick(T0 + 4, task)
        self.assertEqual([b["taskId"] for b in self.puts], [task, None])

    def test_presence_goes_at_once_on_a_change_spaced_on_a_step_and_repeats(self):
        self.r.gate = True
        self.tick(T0 + 2)
        self.main.result(T0 + 3, "t1")
        self.main.call(T0 + 4, tools=[("t2", "Edit", {})])
        self.m.poll(T0 + 4)
        self.tick(T0 + 5)
        self.assertEqual(len(self.sent), 1, "a step change went within 11 s")
        self.tick(T0 + 13)
        self.assertEqual(self.sent[-1].get("step"), "editing_code")
        self.main.result(T0 + 14, "t2")
        self.main.call(T0 + 15, stop="end_turn", text="done")
        self.m.poll(T0 + 15)
        self.tick(T0 + 15)
        self.assertEqual(self.sent[-1], {"state": "idle", "taskId": None}, "a state change waited")
        self.tick(T0 + 54)
        self.assertEqual(len(self.sent), 3)
        self.tick(T0 + 56)
        self.assertEqual(len(self.sent), 4, "no repeat after 40 s")

    def test_idle_goes_quiet_after_15_minutes_and_a_wait_after_30(self):
        self.main.result(T0 + 3, "t1")
        self.main.call(T0 + 4, stop="end_turn", text="done")
        self.m.poll(T0 + 4)
        for t in range(5, 15 * 60 + 4, 20):
            self.tick(T0 + t)
        self.assertEqual(self.quiet, [])
        self.tick(T0 + 4 + 15 * 60)
        count = len(self.sent)
        for t in range(15 * 60 + 10, 40 * 60, 20):
            self.tick(T0 + t)
        self.assertEqual((self.quiet, len(self.sent)), ([True], count), "a quiet session kept repeating")
        # A question revives it; it waits 30 min before going quiet again.
        self.main.prompt(T0 + 2400)
        self.main.call(T0 + 2401, tools=[("q", "AskUserQuestion", {})])
        self.m.poll(T0 + 2401)
        self.tick(T0 + 2402)
        self.assertEqual(self.sent[-1]["state"], "working")
        self.tick(T0 + 2401 + 29 * 60)
        self.assertEqual(len(self.quiet), 1)
        self.tick(T0 + 2401 + 30 * 60)
        self.assertEqual(len(self.quiet), 2)

    def test_work_and_problems_never_go_quiet(self):
        for t in range(2, 3 * 3600, 30):
            self.tick(T0 + t)
        self.main.synthetic(T0 + 3 * 3600, "rate_limit", "You've hit your session limit")
        self.m.poll(T0 + 3 * 3600)
        for t in range(3 * 3600, 6 * 3600, 30):
            self.tick(T0 + t)
        self.assertEqual(self.quiet, [])
        self.assertEqual(self.sent[-1], {"state": "problem", "taskId": None, "reason": "usage_limit"})

    def test_the_row_goes_every_15_s_with_changed_hours_and_a_keepalive_every_10_min(self):
        self.tick(T0 + 2)
        self.assertEqual(self.puts[0]["hours"], [{
            "hour": iso(int(T0 // HOUR) * HOUR), "inputTokens": 1, "outputTokens": 2, "cacheReadTokens": 3,
            "cacheWriteTokens": 4, "activeMs": 2000,  # t1 has run 1 s so far
            "parts": [{"model": "claude-opus-5-5", "agent": "main", "speed": "standard", "longPrompt": False,
                       "calls": 1, "inputTokens": 1, "outputTokens": 2, "cacheReadTokens": 3,
                       "cacheWrite5mTokens": 4, "cacheWrite1hTokens": 0, "thinkingTokens": 0, "webSearches": 0,
                       "webFetches": 0}]}])
        self.assertEqual(fake_api.vine_messages(self.puts[0]), [])
        self.main.result(T0 + 5, "t1")
        self.m.poll(T0 + 5)
        self.tick(T0 + 6)
        self.assertEqual(len(self.puts), 1, "a change went within 15 s")
        self.tick(T0 + 17)
        self.assertEqual(self.puts[-1]["hours"][0]["activeMs"], 5000)
        self.assertNotIn("title", self.puts[-1])
        count = len(self.puts)
        self.main.call(T0 + 20, stop="end_turn", text="done")
        self.m.poll(T0 + 20)
        self.tick(T0 + 21)
        self.assertEqual(len(self.puts), count + 1, "the change at idle was not flushed at once")
        count = len(self.puts)
        for t in range(22, 600 + 20, 5):
            self.tick(T0 + t)
        self.assertEqual(len(self.puts), count, "an unchanged row was sent before the keepalive")
        self.tick(T0 + 21 + 600)
        self.assertEqual(len(self.puts), count + 1)
        self.assertNotIn("hours", self.puts[-1])

    def test_the_body_holds_exactly_the_routes_keys(self):
        self.m.s["title"] = "x"
        self.tick(T0 + 2)
        self.assertEqual(set(self.puts[0]), fake_api.BODY_FIELDS["sessions"])
        self.assertEqual(self.puts[0]["claudeSessionId"], self.session)

    def test_a_refused_token_or_a_taken_id_backs_off_an_hour(self):
        for status, code in ((401, "agent_token_invalid"), (409, "agent_session_taken")):
            with self.subTest(status):
                self.puts = []
                self.r = watch.Reporter(self.m, self.session, self.presence, lambda: None, self.put, "laptop")
                self.answers = [(status, {"code": code})]
                self.tick(T0 + 2)
                for t in range(3, 3600, 30):
                    self.tick(T0 + t)
                self.assertEqual(len(self.puts), 1, "retried within the hour")
                self.tick(T0 + 2 + 3600)
                self.assertEqual(len(self.puts), 2)

    def test_a_network_failure_retries_after_15_s_doubling_to_5_min(self):
        self.answers = [(0, {})] * 7
        tries = []
        for t in range(2, 1100):
            count = len(self.puts)
            self.tick(T0 + t)
            if len(self.puts) > count:
                tries.append(t)
        self.assertEqual([b - a for a, b in zip(tries, tries[1:])][:6], [15, 30, 60, 120, 240, 300])
        self.assertEqual(self.r.r["put"]["failures"], 0, "a success did not reset the backoff")

    def test_a_validation_failure_naming_the_title_or_task_leaves_them_out(self):
        # The backend answers its validator's failures as {error, messages}, with no top-level field.
        task = str(uuid.uuid4())
        self.m.s["title"] = "Status page"
        self.answers = [(422, {"error": "Validation failed", "messages": [
            {"message": "The taskId field must be a valid UUID", "rule": "uuid", "field": "taskId"},
            {"message": "The title field must not be greater than 300 characters", "rule": "maxLength",
             "field": "title"}]})]
        self.tick(T0 + 2, task)
        self.tick(T0 + 4, task)
        self.assertEqual([("title" in b, b["taskId"]) for b in self.puts], [(True, task), (False, None)])

    def test_a_long_title_is_cut_to_the_routes_300_utf16_units(self):
        self.m.s["title"] = "\U0001d400" * 200  # 200 code points, 400 UTF-16 units
        self.tick(T0 + 2)
        self.assertEqual(self.puts[0]["title"], "\U0001d400" * 150)

    def test_at_most_500_hours_go_at_once_and_none_older_than_90_days(self):
        main = self.m.file("main")
        main["firstTs"] = T0 - 100 * 86400
        first = int(main["firstTs"] // HOUR) * HOUR
        self.m.s["hours"] = {str(h): [1, 1, 1, 1] for h in range(first, int(T0), HOUR)}
        self.tick(T0 + 2)
        self.tick(T0 + 20)
        sent = [watch.stamp(h["hour"]) for b in self.puts for h in b.get("hours", [])]
        self.assertEqual([len(b["hours"]) for b in self.puts], [500, 500])
        self.assertGreaterEqual(min(sent), T0 + 2 - 90 * 86400)
        self.assertEqual(len(set(sent)), 1000)


class PartsReport(ReporterCase):
    """When an hour goes again for its parts, and what its parts may hold."""

    def setUp(self):
        super().setUp()
        self.main.result(T0 + 3, "t1")
        self.main.call(T0 + 4, mid="msg_end", use=usage(0, 5, 0, 0), stop="end_turn", text="done")
        self.m.poll(T0 + 4)
        self.hour = str(int(T0 // HOUR) * HOUR)

    def hours_sent(self):
        return [h for b in self.puts for h in b.get("hours", [])]

    def test_an_hour_goes_again_when_only_a_part_grew(self):
        self.tick(T0 + 5)
        [before] = self.hours_sent()
        # A call with no tokens, past the gap that counts as active time: the hour's
        # totals stay, its calls grow.
        self.main.call(T0 + 400, mid="msg_empty", stop="end_turn", text="ok")
        self.m.poll(T0 + 400)
        self.tick(T0 + 401)
        [hour] = self.puts[-1]["hours"]
        self.assertEqual({k: v for k, v in hour.items() if k != "parts"},
                         {k: v for k, v in before.items() if k != "parts"})
        self.assertEqual([p["calls"] for p in hour["parts"]], [3])

    def test_an_unchanged_hour_is_not_sent_again_even_after_a_restart(self):
        self.tick(T0 + 5)
        for t in range(6, 1300, 5):
            self.tick(T0 + t)
        self.assertGreater(len(self.puts), 2, "no keepalive went")
        self.assertEqual(len(self.hours_sent()), 1, "an unchanged hour went again")
        saved = json.loads(json.dumps(self.r.r))
        self.r = watch.Reporter(self.m, self.session, self.presence, lambda: None, self.put, "laptop", saved)
        self.tick(T0 + 1300)
        self.assertEqual(len(self.hours_sent()), 1, "a restarted reporter sent the hour again")

    def test_a_report_state_saved_before_parts_sends_each_hour_once_more_with_them(self):
        sent = {"hours": {self.hour: self.m.buckets(T0 + 5)[int(self.hour)]}, "fields": None, "title": None}
        self.r = watch.Reporter(self.m, self.session, self.presence, lambda: None, self.put, "laptop", {"sent": sent})
        self.tick(T0 + 5)
        self.tick(T0 + 30)
        [hour] = self.hours_sent()
        self.assertEqual((hour["outputTokens"], hour["parts"][0]["calls"]), (7, 2))

    def test_at_most_32_parts_the_largest_each_number_capped(self):
        self.m.s["hours"][self.hour] = [10 ** 11, 10 ** 11, 0, 0]
        self.m.s["parts"][self.hour] = {watch.part_key(f"claude-test-{n}", "subagent", "standard", False):
                                        [1, n, 0, 0, 0, 0, 0, 0, 0] for n in range(1, 41)}
        self.m.s["parts"][self.hour][watch.part_key("claude-big", "main", "fast", True)] = \
            [2_000_000, 5, 3 * 10 ** 10, 0, 0, 0, 4 * 10 ** 10, 3_000_000, 4_000_000]
        self.tick(T0 + 5)
        self.assertEqual(fake_api.vine_messages(self.puts[0]), [])
        [hour] = self.puts[0]["hours"]
        self.assertEqual({p["model"] for p in hour["parts"]},
                         {"claude-big", *(f"claude-test-{n}" for n in range(10, 41))})
        big = next(p for p in hour["parts"] if p["model"] == "claude-big")
        self.assertEqual(big, {"model": "claude-big", "agent": "main", "speed": "fast", "longPrompt": True,
                               "calls": 1_000_000, "inputTokens": 5, "outputTokens": 10 ** 10, "cacheReadTokens": 0,
                               "cacheWrite5mTokens": 0, "cacheWrite1hTokens": 0, "thinkingTokens": 10 ** 10,
                               "webSearches": 1_000_000, "webFetches": 1_000_000})

    def test_parts_never_sum_past_the_hours_totals(self):
        self.m.s["hours"][self.hour] = [10, 10, 10, 10]
        self.m.s["parts"][self.hour] = {watch.part_key(model, "main", "standard", False): [1, 8, 8, 8, 4, 4, 0, 0, 0]
                                        for model in ("claude-a", "claude-b")}
        self.tick(T0 + 5)
        [hour] = self.puts[0]["hours"]
        for total, fields in (("inputTokens", ["inputTokens"]), ("outputTokens", ["outputTokens"]),
                              ("cacheReadTokens", ["cacheReadTokens"]),
                              ("cacheWriteTokens", ["cacheWrite5mTokens", "cacheWrite1hTokens"])):
            self.assertEqual(sum(p[f] for p in hour["parts"] for f in fields), hour[total], total)

    def test_parts_the_server_refuses_are_left_out_and_the_hours_sent_again(self):
        self.answers = [(422, {"error": "Validation failed", "messages": [
            {"message": "The model field format is invalid", "rule": "regex", "field": "hours.0.parts.0.model"}]})]
        self.tick(T0 + 5)
        self.tick(T0 + 6)
        self.assertEqual([("parts" in h, h["outputTokens"]) for h in self.hours_sent()], [(True, 7), (False, 7)])
        self.tick(T0 + 30)
        self.assertEqual(len(self.hours_sent()), 2)


class Ghost(unittest.TestCase):
    """Which other watcher on the same Claude Code process ends this one, in process."""

    PID, PID_START = 4242, "99"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-watch-ghost-"))
        self.addCleanup(remove_tree, self.tmp)
        patch = mock.patch.object(session_hook, "STATE", self.tmp / "state")
        patch.start()
        self.addCleanup(patch.stop)
        self.hook = watch.load_hook()

    def watcher(self, session, reexec=False):
        args = argparse.Namespace(session=session, pid=self.PID, pid_start=self.PID_START, source="", model=None,
                                  transcript=str(self.tmp / f"{session}.jsonl"), event_at=None, reexec=reexec)
        return watch.Watcher(args, self.hook)

    def other(self, **fields):
        state = {"v": watch.STATE_VERSION, "session": "other", "pid": self.PID, "pidStart": self.PID_START,
                 "boundAt": time.time() + 5, "savedAt": time.time(), "ended": False, **fields}
        self.hook.STATE.mkdir(parents=True, exist_ok=True)
        (self.hook.STATE / "other.watch.json").write_text(json.dumps(state))

    def test_a_watcher_bound_later_to_the_same_process_makes_this_one_a_ghost(self):
        w = self.watcher(str(uuid.uuid4()))
        self.other()
        self.assertTrue(w.ghost())

    def test_one_that_ended_or_died_does_not(self):
        w = self.watcher(str(uuid.uuid4()))
        for fields in ({"ended": True}, {"savedAt": time.time() - 3600}):
            with self.subTest(fields):
                self.other(**fields)
                self.assertFalse(w.ghost())

    def test_a_start_binds_the_session_anew_and_a_reexec_does_not(self):
        session = str(uuid.uuid4())
        first = self.watcher(session)
        first.save(force=True)
        time.sleep(0.01)
        again = self.watcher(session)
        self.assertGreater(again.meta["boundAt"], first.meta["boundAt"])
        again.save(force=True)
        self.assertEqual(self.watcher(session, reexec=True).meta["boundAt"], again.meta["boundAt"])


class ProcessCase(unittest.TestCase):
    """The watcher as a process, against the fake API."""

    TIMES = {"tick": 0.2, "list": 0.3, "endAfter": 1.5, "ghostEvery": 0.3, "saveEvery": 0.2, "stepSpacing": 1,
             "presenceRepeat": 2, "putSpacing": 0.3}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-watch-proc-"))
        self.addCleanup(remove_tree, self.tmp)
        self.token_file = self.tmp / "token"
        self.token_file.write_text(fake_api.TOKEN + "\n")
        self.api = fake_api.FakeApi()
        self.api.start()
        self.addCleanup(self.api.stop)
        self.session = str(uuid.uuid4())
        self.presence_id = watch.presence_id(self.session)
        self.sessions = self.tmp / "state/unio/sessions"
        self.projects = self.tmp / ".claude/projects/-project"
        self.main = Transcript(self.projects / f"{self.session}.jsonl", self.session)
        # A copy, so a test can change the code a watcher runs.
        self.code = self.tmp / "scripts"
        shutil.copytree(SCRIPTS / "unio_sessions", self.code / "unio_sessions",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(SCRIPTS / "sessions.py", self.code / "sessions.py")
        self.entry = self.code / "sessions.py"
        self.mine = []
        self.addCleanup(self.stop_all)

    def env(self, **times):
        return base_env(self.tmp, UNIO_SESSION_TOKEN_FILE=str(self.token_file), UNIO_SESSION_API=self.api.base,
                        CLAUDE_CODE_EXECPATH="/home/me/.local/share/claude/versions/2.1.291",
                        UNIO_WATCH_TIMES=json.dumps({**self.TIMES, **times}))

    def claude(self):
        """A stand-in Claude Code process; the watcher only follows its pid and start time."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.mine.append(proc)
        return proc

    @staticmethod
    def start_time(pid):
        return system.start_time(pid)

    def watcher(self, claude, session=None, source="startup", transcript=None, **times):
        session = session or self.session
        args = [sys.executable, "-I", str(self.entry), "watch", "--session", session, "--pid", str(claude.pid),
                "--pid-start", self.start_time(claude.pid), "--source", source,
                "--transcript", str(transcript or self.projects / f"{session}.jsonl")]
        proc = subprocess.Popen(args, env=self.env(**times), stdin=subprocess.DEVNULL)
        self.mine.append(proc)
        return proc

    def stop_all(self):
        for proc in reversed(self.mine):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    def wait_for(self, check, limit, message):
        deadline = time.monotonic() + limit * SLACK
        while True:
            value = check()
            if value:
                return value
            if time.monotonic() > deadline:
                self.fail(message() if callable(message) else message)
            time.sleep(0.05)

    def state(self, session=None):
        try:
            return json.loads((self.sessions / f"{session or self.session}.watch.json").read_text())
        except (OSError, ValueError):
            return {}

    def hook_end(self, session=None):
        """SessionEnd as the hook reports it: `ended` in presence.json, sent by its own sender."""
        data = {"session_id": session or self.session, "hook_event_name": "SessionEnd", "reason": "other"}
        subprocess.run([sys.executable, "-I", str(self.entry), "hook"], input=json.dumps(data), text=True,
                       env=self.env(), timeout=20, capture_output=True)

    def bodies(self):
        return [r["body"] for r in self.api.presence_calls()]

    def totals(self):
        return self.api.session_totals(self.presence_id)

    def expected(self, files):
        """The independent count over `files`: last entry per message.id, no <synthetic>."""
        out = {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0}
        for path in files:
            last = {}
            with Path(path).open("rb") as fh:
                lines = fh.readlines()
            for line in lines:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message") or {}
                counted = m.get("model") != "<synthetic>" and isinstance(m.get("usage"), dict)
                if e.get("type") == "assistant" and counted:
                    last[m.get("id") or e.get("uuid")] = m["usage"]
            for u in last.values():
                out["inputTokens"] += u.get("input_tokens") or 0
                out["outputTokens"] += u.get("output_tokens") or 0
                out["cacheReadTokens"] += u.get("cache_read_input_tokens") or 0
                out["cacheWriteTokens"] += u.get("cache_creation_input_tokens") or 0
        return out

    def counted(self):
        return {k: v for k, v in self.totals().items() if k != "activeMs"}

    def expected_parts(self, files):
        """independent_parts over `files`, summed over hours as the fake API keys them."""
        out = {}
        for parts in independent_parts(self.main.path, files).values():
            for key, v in parts.items():
                model, agent, speed, long_prompt = key.split(" ")
                sums = out.setdefault((model, agent, speed, long_prompt == "1"), [0] * len(v))
                for i, x in enumerate(v):
                    sums[i] += x
        return {k: dict(zip(fake_api.PART_LIMITS, v)) for k, v in out.items()}

    def agent(self, agent_id, run=None, tool_use=None):
        return Rules.agent(self, agent_id, run, tool_use)

    def journal(self, run, *events):
        return Rules.journal(self, run, *events)

    @property
    def main_path(self):
        return self.main.path


class Process(ProcessCase):
    def test_a_busy_session_reaches_the_server_whole(self):
        start = time.time() - 5400  # the background agent's last call is 5105 s in
        busy_session(self.main, self.agent, self.journal, start)
        files = [self.main.path, *sorted(self.main.path.with_suffix("").rglob("agent-*.jsonl"))]
        claude = self.claude()
        self.watcher(claude)
        expected = self.expected(files)
        self.wait_for(lambda: self.counted() == expected, 60,
                      lambda: f"server {self.counted()} != independent {expected}; "
                              f"last write {self.api.session_writes[-1:]}")
        parts = self.expected_parts(files)
        self.wait_for(lambda: self.api.session_parts(self.presence_id) == parts, 20,
                      lambda: f"server {self.api.session_parts(self.presence_id)} != independent {parts}")
        self.assertEqual([w for w in self.api.session_writes if w["status"] != 200], [])
        row = self.api.sessions[self.presence_id]
        self.assertEqual((row["model"], row["contextLimit"], row["countedSince"]), ("claude-opus-5-5", 200_000, None))
        self.assertAlmostEqual(row["startedAt"].timestamp(), watch.Model.first_stamp(self.main.path), places=2)
        self.assertGreater(self.totals()["activeMs"], 0)

    def test_the_cursor_survives_a_restart_without_counting_twice(self):
        self.main.prompt(time.time() - 60)
        self.main.call(time.time() - 50, use=usage(1, 10, 100, 1000), tools=[("t1", "Read", {})])
        claude = self.claude()
        first = self.watcher(claude)
        self.wait_for(lambda: self.counted()["outputTokens"] == 10, 20, "the first report never landed")
        self.wait_for(lambda: self.state().get("model", {}).get("files", {}).get("main", {}).get("offset"), 10,
                      "no cursor was saved")
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=10)
        self.main.result(time.time() - 5, "t1")
        self.main.call(time.time() - 4, use=usage(2, 20, 200, 2000), stop="end_turn", text="done")
        self.watcher(claude, source="")
        files = [self.main.path]
        self.wait_for(lambda: self.counted() == self.expected(files), 20,
                      lambda: f"{self.counted()} != {self.expected(files)}")
        time.sleep(1)
        self.assertEqual(self.counted(), {"inputTokens": 3, "outputTokens": 30, "cacheReadTokens": 300,
                                          "cacheWriteTokens": 3000})
        self.assertIsNone(self.api.sessions[self.presence_id]["countedSince"])

    def test_state_saved_before_parts_resumes_and_reports_them_from_then_on(self):
        self.main.prompt(time.time() - 60)
        self.main.call(time.time() - 50, mid="msg_a", use=usage(1, 10, 100, 1000), tools=[("t1", "Read", {})])
        claude = self.claude()
        first = self.watcher(claude)
        self.wait_for(lambda: self.state().get("report", {}).get("sent", {}).get("parts"), 20,
                      "the first report's parts were never saved")
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=10)
        saved = self.state()
        del saved["model"]["parts"], saved["report"]["sent"]["parts"]
        for f in saved["model"]["files"].values():
            del f["lastMessagePart"], f["lastMessageDetail"]
        (self.sessions / f"{self.session}.watch.json").write_text(json.dumps(saved))
        self.main.result(time.time() - 5, "t1")
        self.main.call(time.time() - 4, mid="msg_b", use=usage(2, 20, 200, 2000), model="claude-sonnet-5-5",
                       stop="end_turn", text="done")
        self.watcher(claude, source="")
        sonnet = ("claude-sonnet-5-5", "main", "standard", False)
        self.wait_for(lambda: sonnet in self.api.session_parts(self.presence_id), 20,
                      lambda: f"no part for the new call: {self.state().get('lastError')}")
        self.assertEqual(self.counted(), {"inputTokens": 3, "outputTokens": 30, "cacheReadTokens": 300,
                                          "cacheWriteTokens": 3000})
        self.assertEqual(self.api.session_parts(self.presence_id)[sonnet], dict(
            zip(fake_api.PART_LIMITS, [1, 2, 20, 200, 2000, 0, 0, 0, 0])))
        self.assertNotIn("lastError", self.state())

    def test_a_session_already_running_is_counted_from_now(self):
        self.main.prompt(time.time() - 3 * 3600)
        self.main.call(time.time() - 3 * 3600 + 5, use=usage(5, 50, 500, 5000), stop="end_turn", text="old")
        claude = self.claude()
        before = time.time()
        self.watcher(claude, source="resume")
        self.wait_for(lambda: self.api.sessions.get(self.presence_id), 20, "no row")
        row = self.api.sessions[self.presence_id]
        self.assertGreaterEqual(row["countedSince"].timestamp(), before - 1)
        self.assertEqual(self.counted(), {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
                                          "cacheWriteTokens": 0})
        self.main.prompt(time.time())
        self.main.call(time.time(), use=usage(1, 1, 1, 1), stop="end_turn", text="new")
        self.wait_for(lambda: self.counted()["outputTokens"] == 1, 20, "the new call was not counted")

    @unittest.skipIf(WINDOWS, "Windows has no exec that keeps the process and its lock")
    def test_it_reexecs_when_its_code_changes_and_compiles(self):
        self.main.prompt(time.time())
        claude = self.claude()
        proc = self.watcher(claude)
        self.wait_for(lambda: self.state().get("report"), 20, "the watcher never saved")
        hook = self.code / "unio_sessions" / "hook.py"
        changed = time.time()
        hook.write_text(hook.read_text() + "\n# a later version\n")
        self.wait_for(lambda: self.state().get("execs") == 1 and self.state().get("imageAt", 0) > changed, 20,
                      "no re-exec after the hook changed")
        self.assertIsNone(proc.poll())
        broken = self.code / "unio_sessions" / "watch.py"
        broken.write_text(broken.read_text() + "\ndef broken(:\n")
        time.sleep(2)
        self.assertEqual(self.state().get("execs"), 1, "it re-exec'd into code that does not compile")
        self.assertIsNone(proc.poll())
        self.main.call(time.time(), use=usage(1, 7, 1, 1), stop="end_turn", text="still counting")
        self.wait_for(lambda: self.counted()["outputTokens"] == 7, 20, "the re-exec'd watcher stopped counting")

    def test_one_watcher_per_session_at_low_priority(self):
        self.main.prompt(time.time())
        claude = self.claude()
        first = self.watcher(claude)
        self.wait_for(lambda: self.state().get("report"), 20, "the first watcher never saved")
        second = self.watcher(claude)
        self.assertEqual(second.wait(timeout=20), 0)
        self.assertIsNone(first.poll())
        if WINDOWS:
            return  # started below normal priority: test_system checks the flags
        nice = os.getpriority(os.PRIO_PROCESS, first.pid)
        self.assertGreaterEqual(nice, 10)
        if sys.platform.startswith("linux") and shutil.which("ionice"):
            out = subprocess.run(["ionice", "-p", str(first.pid)], capture_output=True, text=True).stdout
            self.assertEqual(out.strip(), "best-effort: prio 7")

    def test_an_end_from_before_a_resume_does_not_end_the_new_run(self):
        self.sessions.mkdir(parents=True)
        (self.sessions / f"{self.session}.presence.json").write_text(json.dumps(
            {"want": {"state": "ended"}, "seq": 4, "requestedAt": time.time() - 3600, "sent": {"state": "ended"},
             "sentAt": time.time() - 3600, "lastStatus": 200, "lastSeq": 4}))
        self.main.prompt(time.time())
        claude = self.claude()
        proc = self.watcher(claude, source="resume")
        self.wait_for(lambda: self.bodies() and self.bodies()[-1]["state"] == "working", 20,
                      "the old end stopped the resumed session")
        self.assertIsNone(proc.poll())
        self.hook_end()
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertEqual(self.state().get("endedBy"), "session_end")

    def test_two_processes_on_one_id_end_only_when_the_transcript_rests(self):
        self.main.prompt(time.time())
        first, other = self.claude(), self.claude()
        proc = self.watcher(first)
        self.wait_for(lambda: self.api.sessions.get(self.presence_id), 20, "no row")
        first.kill()
        first.wait()
        # The other process keeps writing to the same transcript.
        for n in range(8):
            self.main.call(time.time(), use=usage(0, 1, 0, 0), stop="end_turn", text=str(n))
            time.sleep(0.5)
        self.assertIsNone(proc.poll(), "it ended while the session was still written")
        self.wait_for(lambda: proc.poll() is not None, 20, "it never ended once the transcript rested")
        self.assertEqual(self.state().get("endedBy"), "process_gone")
        self.assertIsNotNone(self.api.sessions[self.presence_id]["endedAt"])
        self.assertIsNone(other.poll())

    def test_a_clear_leaves_no_ghost(self):
        # /clear starts a new session id in the same process; when the old one's
        # SessionEnd never ran, its watcher ends itself on seeing the new one.
        self.main.prompt(time.time())
        claude = self.claude()
        old = self.watcher(claude)
        self.wait_for(lambda: self.state().get("report"), 20, "the first watcher never saved")
        new_session = str(uuid.uuid4())
        Transcript(self.projects / f"{new_session}.jsonl", new_session).prompt(time.time())
        self.watcher(claude, session=new_session, source="clear")
        self.wait_for(lambda: old.poll() is not None, 20, "the old session's watcher lives on as a ghost")
        self.assertEqual(self.state().get("endedBy"), "cleared")
        self.assertEqual(self.bodies()[0]["state"], "working")
        self.assertIn("ended", [b["state"] for b in self.bodies()])
        self.assertEqual(self.state(new_session).get("ended"), False)

    def test_a_session_the_process_comes_back_to_keeps_reporting(self):
        # /clear makes session B in A's process, then /resume brings A back into it.
        self.main.prompt(time.time())
        claude = self.claude()
        first = self.watcher(claude)
        self.wait_for(lambda: self.state().get("report"), 20, "A's first watcher never saved")
        b = str(uuid.uuid4())
        Transcript(self.projects / f"{b}.jsonl", b).prompt(time.time())
        self.watcher(claude, session=b, source="clear")
        self.wait_for(lambda: first.poll() is not None, 20, "A's watcher did not end on the clear")
        self.hook_end(session=b)
        self.wait_for(lambda: self.state(b).get("ended") is True, 20, "B never ended")
        self.main.prompt(time.time())
        again = self.watcher(claude, source="resume")
        self.wait_for(lambda: self.state().get("ended") is False and "endedBy" not in self.state(), 20,
                      "A's watcher did not start again")
        self.wait_for(lambda: self.api.sessions[self.presence_id]["endedAt"] is None, 20, "A's row stayed ended")
        time.sleep(2)
        self.assertIsNone(again.poll(), f"A's watcher ended: {self.state().get('endedBy')}")
        mine = [body["state"] for call, body in zip(self.api.presence_calls(), self.bodies())
                if call["path"].endswith(self.presence_id)]
        self.assertEqual(mine[-1], "working")

    def test_a_continued_sessions_copies_written_after_the_watcher_started_are_not_counted(self):
        # The file appears holding the conversation it continues, stamped from that
        # one's first prompt on, then its own entries.
        claude = self.claude()
        self.watcher(claude, source="resume")
        self.wait_for(lambda: self.state().get("report"), 20, "the watcher never saved")
        old = time.time() - 3 * 3600
        self.main.prompt(old)
        self.main.call(old + 5, mid="msg_copied", use=usage(1, 1000, 5_000_000, 100), stop="end_turn", text="x")
        self.main.prompt(time.time())
        self.main.call(time.time(), mid="msg_own", use=usage(1, 7, 50, 1), stop="end_turn", text="y")
        self.wait_for(lambda: self.counted()["outputTokens"], 20, "the session's own call never landed")
        time.sleep(1)
        self.assertEqual(self.counted(), {"inputTokens": 1, "outputTokens": 7, "cacheReadTokens": 50,
                                          "cacheWriteTokens": 1})
        row = self.api.sessions[self.presence_id]
        self.assertGreater(row["startedAt"].timestamp(), old + 3600)
        self.assertIsNone(row["countedSince"])

    def test_session_end_flushes_the_row_and_stops(self):
        self.main.prompt(time.time())
        claude = self.claude()
        proc = self.watcher(claude)
        self.wait_for(lambda: self.api.sessions.get(self.presence_id), 20, "no row")
        self.hook_end()
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertIsNotNone(self.api.sessions[self.presence_id]["endedAt"])
        self.assertEqual(self.state().get("endedBy"), "session_end")

    def test_an_older_server_gets_no_new_steps(self):
        self.api.sessions_status = 403
        self.main.prompt(time.time())
        self.main.call(time.time(), tools=[("t1", "Bash", {"command": "pnpm test"})])
        claude = self.claude()
        self.watcher(claude)
        # The same state twice: the hook sends the repeat once 11 s have passed.
        self.wait_for(lambda: len(self.bodies()) >= 2, 30, "presence stopped")
        self.assertEqual(len(self.api.session_calls()), 1)
        self.assertEqual({b.get("step") for b in self.bodies()}, {None})
        self.assertGreater(self.state()["report"]["put"]["backoffUntil"], time.time() + 3000)

    def test_a_newer_server_gets_the_step(self):
        self.main.prompt(time.time())
        self.main.call(time.time(), tools=[("t1", "Bash", {"command": "pnpm test"})])
        claude = self.claude()
        self.watcher(claude)
        self.wait_for(lambda: any(b.get("step") == "running_tests" for b in self.bodies()), 20,
                      lambda: f"no step: {self.bodies()}")

    def test_going_quiet_clears_want_and_stops_the_repeats(self):
        self.main.prompt(time.time() - 30)
        self.main.call(time.time() - 29, stop="end_turn", text="done")
        claude = self.claude()
        self.watcher(claude, quietIdle=2)
        presence = self.sessions / f"{self.session}.presence.json"
        self.wait_for(lambda: presence.exists() and "quietAt" in json.loads(presence.read_text()), 20,
                      "it never went quiet")
        self.assertNotIn("want", json.loads(presence.read_text()))
        count = len(self.bodies())
        time.sleep(3)
        self.assertEqual(len(self.bodies()), count, "a quiet session kept repeating")
        self.main.prompt(time.time())
        self.wait_for(lambda: len(self.bodies()) > count and self.bodies()[-1]["state"] == "working", 20,
                      "new activity did not bring the session back")


if __name__ == "__main__":
    unittest.main()
