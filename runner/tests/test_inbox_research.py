"""Research on mention: the steps web tools report, the stream the runner
reads, and the research profile's limits, gates and endings."""
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from inbox_support import inbox

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeProc:
    def __init__(self, lines):
        self.stdout = [line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode() for line in lines]
        self.pid = None
        self.stopped = 0


def read(lines, session=None):
    """The session the runner's reader makes of these stream lines."""
    s = session or inbox.Session(required=(), builtin={"Agent", "Task", "WebSearch", "WebFetch"})
    s.allowed = None
    proc = FakeProc(lines)
    # Never a real signal: the reader stops a session through stop_group.
    with mock.patch.object(inbox, "stop_group", lambda p, force=False: setattr(p, "stopped", p.stopped + 1)):
        inbox.Runner._read(proc, s, io.BytesIO())
    s.proc = proc
    return s


def fixture(name):
    return (FIXTURES / name).read_bytes().splitlines(keepends=True)


class Steps(unittest.TestCase):
    def test_web_tools_report_researching(self):
        # The server has known this code since the coding-session watcher (DEV-439).
        self.assertEqual(inbox.STEPS["WebSearch"], "researching")
        self.assertEqual(inbox.STEPS["WebFetch"], "researching")


class Subagents(unittest.TestCase):
    """The probe streams of 2026-10-27 (Claude Code 2.1.311, paths removed):
    a general-purpose subagent called the parent's MCP tool, tagged with
    parent_tool_use_id."""

    def test_a_subagent_write_never_counts(self):
        for name in ("subagent-p1.jsonl", "subagent-p2.jsonl"):
            s = read(fixture(name))
            self.assertNotIn("mcp__probe__ping", s.succeeded, name)
            self.assertNotIn("mcp__probe__ping", s.tool_uses.values(), name)
            self.assertIn("Agent", s.tool_uses.values(), name)
            self.assertIsNotNone(s.result, name)

    def test_a_subagent_never_moves_the_step(self):
        s = inbox.Session(required=(), steps={"mcp__probe__ping": "writing_reply"}, builtin={"Agent", "Task"})
        read(fixture("subagent-p2.jsonl"), s)
        self.assertIsNone(s.step)

    def test_the_parent_still_counts(self):
        s = read([
            {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_use", "id": "a", "name": "mcp__unio__create_comment", "input": {}}]}},
            {"type": "user", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "{}"}]}},
        ])
        self.assertEqual(s.succeeded, {"mcp__unio__create_comment"})


class Quota(unittest.TestCase):
    def test_both_windows_from_one_event(self):
        s = read(fixture("subagent-p1.jsonl"))
        self.assertEqual(s.rate_limits["seven_day"], {"utilization": 0.89, "resetsAt": "2026-10-28T12:00:00Z"})
        self.assertEqual(s.rate_limits["five_hour"]["utilization"], 0.19)

    def test_one_window_without_unified_windows(self):
        event = {"type": "rate_limit_event", "rate_limit_info": {
            "rateLimitType": "five_hour", "utilization": 0.93, "resetsAt": 1793117400}}
        self.assertEqual(inbox.rate_limits(event), {"five_hour": {"utilization": 0.93,
                                                                  "resetsAt": "2026-10-27T16:10:00Z"}})
        self.assertEqual(inbox.rate_limits({"rate_limit_info": {"rateLimitType": "seven_day",
                                                                "utilization": "high"}}), {})

    def test_health_keeps_a_fresh_reading_only(self):
        s = inbox.Session()
        s.rate_limits = {"seven_day": {"utilization": 0.91, "resetsAt": "2026-10-28T00:00:00Z"}}
        h = {}
        at = inbox.parse_iso("2026-10-27T12:00:00Z")
        inbox.note_quota(h, s, at)
        self.assertEqual(h["quota"]["seven_day"]["at"], "2026-10-27T12:00:00Z")
        used = inbox.quota_used(h, "seven_day", at + inbox.timedelta(minutes=29))
        self.assertEqual(used, (0.91, inbox.parse_iso("2026-10-28T00:00:00Z")))
        self.assertIsNone(inbox.quota_used(h, "seven_day", at + inbox.timedelta(minutes=31)))
        self.assertIsNone(inbox.quota_used(h, "five_hour", at))


def use(n, name, args, result=None, structured=None, error=False):
    """A tool call and its answer, as the stream prints them."""
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{n}", "name": name, "input": args}]}}]
    answer = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{n}", "is_error": error, "content": result or "ok"}]}}
    if structured is not None:
        answer["tool_use_result"] = structured
    return lines + [answer]


def fetch(n, url, code=200, final=None):
    return use(n, "WebFetch", {"url": url, "prompt": "p"},
               structured={"bytes": 10, "code": code, "codeText": "OK", "result": "r", "url": final or url})


class Usage(unittest.TestCase):
    def test_web_counts(self):
        s = read([*use(1, "WebSearch", {"query": "aliquota iss", "mode": "standard"}),
                  *fetch(2, "https://www.planalto.gov.br/a"), *fetch(3, "https://portal.stf.jus.br/t", 504),
                  *fetch(4, "https://www.planalto.gov.br/a"),
                  *use(5, "WebFetch", {"url": "https://x.example/"}, "connect ECONNREFUSED", "Error: x", True)])
        self.assertEqual(s.web.counts(), {"searches": 1, "fetches": 4, "sources": 1})
        self.assertEqual(s.web.sources, ["https://www.planalto.gov.br/a"])
        self.assertEqual(s.step, "researching")

    def test_a_redirect_is_kept_where_it_landed(self):
        s = read(fetch(1, "https://a.example/old", 200, "https://b.example/new"))
        self.assertEqual(s.web.sources, ["https://b.example/new"])

    def test_usage_from_the_result(self):
        result = json.loads(fixture("subagent-p1.jsonl")[-1])
        usage = inbox.run_usage(result)
        self.assertEqual(usage["model"], "claude-opus-5-5")
        self.assertEqual(usage["turns"], 3)
        self.assertEqual(usage["costUsd"], result["total_cost_usd"])
        self.assertFalse(usage["estimated"])
        self.assertGreater(usage["inputTokens"], 0)
        self.assertEqual(inbox.run_usage({}, 3.0), {"costUsd": 3.0, "estimated": True})
        self.assertIsNone(inbox.run_usage(None))
