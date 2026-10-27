"""Research on mention: the steps web tools report, the stream the runner
reads, and the research profile's limits, gates and endings."""
import io
import json
import os
import signal
import unittest
from datetime import timedelta
from pathlib import Path, PureWindowsPath
from unittest import mock

from inbox_support import FakeUnio, HomeCase, inbox, write_fake_claude

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
                  *fetch(2, "https://www.gov.example/a"), *fetch(3, "https://portal.court.example/t", 504),
                  *fetch(4, "https://www.gov.example/a"),
                  *use(5, "WebFetch", {"url": "https://x.example/"}, "connect ECONNREFUSED", "Error: x", True)])
        self.assertEqual(s.web.counts(), {"searches": 1, "fetches": 4, "sources": 1})
        self.assertEqual(s.web.sources, ["https://www.gov.example/a"])
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


class Intent(unittest.TestCase):
    def test_asking_words_in_portuguese_and_english(self):
        for text in ("@[Unio Bot](user_agent) pesquise as alíquotas de ISS", "Pesquisar o prazo de recurso",
                     "@[Unio Bot](user_agent), levante os julgados", "INVESTIGUE o caso", "apure isso",
                     "descubra quem publica", "compare as duas leis", "pesquisa: taxa Selic",
                     "@[Unio Bot](user_agent) research the vendors", "Look up the API limits",
                     "find out who maintains it", "investigate the outage", "**Pesquíse** a norma"):
            self.assertTrue(inbox.asks_research(text), text)

    def test_other_wording_goes_to_the_reply_run(self):
        for text in ("@[Unio Bot](user_agent) pode explicar?", "obrigado!", "the research is done",
                     "pesquisador novo no time", "comparecer à audiência", "", None,
                     "@[Unio Bot](user_agent) você pode pesquisar isso?"):
            self.assertFalse(inbox.asks_research(text), text)

    def test_escalation_line(self):
        self.assertEqual(inbox.parse_escalation('x\n{"outcome":"research","question":"ISS rates"}'), "ISS rates")
        self.assertEqual(inbox.parse_escalation('{"outcome":"research"}'), "")
        self.assertEqual(len(inbox.parse_escalation('{"outcome":"research","question":"%s"}' % ("a" * 400))),
                         inbox.RESEARCH_QUESTION_CHARS)
        self.assertIsNone(inbox.parse_escalation('{"outcome":"replied"}'))

    def test_escalation_after_a_write_is_refused(self):
        self.assertEqual(inbox.check_outcome("research", set()), ("research", None))
        outcome, error = inbox.check_outcome("research", {"mcp__unio__create_comment"})
        self.assertIsNone(outcome)
        self.assertIn("after writing", error)


def mention(content="@[Agent](user_agent) pesquise as alíquotas de ISS", root=None):
    return {
        "key": "comment:c-0001", "kind": "mention", "subject": "task", "attempts": 0,
        "createdAt": "2026-10-23T12:00:00.000Z",
        "task": {"id": "11111111-1111-4111-8111-111111111111", "key": "ABC-1",
                 "url": "https://unio.so/app/tasks/ABC-1/titulo?ws=org_test", "title": "Título",
                 "team": {"id": 1, "name": "Product", "identifier": "ABC"},
                 "status": {"name": "Todo", "category": "unstarted"}},
        "comment": {"id": "c-0001", "rootId": root or "c-0001", "content": content,
                    "createdAt": "2026-10-23T12:00:00.000Z",
                    "author": {"kind": "user", "name": "Person Marker", "clerkId": "user_person",
                               "canUpdateTasks": True, "canCreateTasks": True}},
    }


class Profile(HomeCase):
    def runner(self, **env):
        self.profile("org_test")
        cfg = inbox.Config({**os.environ, "UNIO_INBOX_WEB": "on", **env}, "org_test")
        return inbox.Runner(cfg)

    def test_argv(self):
        r = self.runner(UNIO_INBOX_RESEARCH_MODEL="claude-sonnet-test")
        argv = r.research_args(Path("/state/mcp/x.json"), ("WebSearch", "WebFetch"))
        self.assertEqual(argv[1:8], ["-p", "--restricted", "--output-format", "stream-json", "--verbose",
                                     "--no-session-persistence", "--settings"])
        value = {flag: argv[argv.index(flag) + 1] for flag in (
            "--tools", "--allowedTools", "--max-turns", "--max-budget-usd", "--model", "--append-system-prompt-file")}
        self.assertEqual(value["--tools"], "WebSearch,WebFetch")
        self.assertEqual(value["--allowedTools"], "mcp__unio__get_task,mcp__unio__list_comments,"
                                                  "mcp__unio__create_comment,mcp__unio__update_comment,"
                                                  "mcp__unio__add_reaction,WebSearch,WebFetch")
        self.assertEqual((value["--max-turns"], value["--max-budget-usd"], value["--model"]),
                         ("60", "3", "claude-sonnet-test"))
        self.assertEqual(value["--append-system-prompt-file"], str(inbox.PROTOCOL_DIR / "researcher.md"))
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        for tool in ("mcp__unio__update_task", "mcp__unio__create_task", "mcp__unio__create_project"):
            self.assertIn(tool, denied)
        self.assertNotIn("mcp__unio__create_comment", denied)
        self.assertNotIn("--add-dir", argv)
        self.assertEqual(argv[-1], "claude-sonnet-test", "the prompt goes on stdin")

    def test_argv_without_the_web(self):
        argv = self.runner().research_args(Path("/state/mcp/x.json"), ())
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("WebSearch", argv[argv.index("--allowedTools") + 1])

    def test_reply_runs_have_no_web(self):
        argv = self.runner().claude_args(Path("/state/mcp/x.json"), [])
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__unio__*")

    def test_limits_from_the_environment(self):
        cfg = self.runner(UNIO_INBOX_RESEARCH_MAX_TURNS="80", UNIO_INBOX_RESEARCH_MAX_BUDGET_USD="2.5",
                          UNIO_INBOX_RESEARCH_DAILY_RUNS="3").cfg
        self.assertEqual((cfg.research_max_turns, cfg.research_max_budget, cfg.research_daily_runs), (80, "2.5", 3))
        cfg = self.runner(UNIO_INBOX_RESEARCH_MAX_TURNS="many").cfg
        self.assertEqual(cfg.research_max_turns, 60)
        self.assertIn("UNIO_INBOX_RESEARCH_MAX_TURNS", cfg.research_error)

    def test_who_goes_to_research(self):
        r = self.runner()
        self.assertEqual(r.research_ask(mention(), {}), {"why": "intent"})
        self.assertIsNone(r.research_ask(mention("@[Agent](user_agent) pode explicar?"), {}))
        threads = {"researchThreads": {"c-root": {"comment": "c-answer", "at": "2026-10-27T12:00:00Z"}}}
        self.assertEqual(r.research_ask(mention("e para Curitiba?", root="c-root"), threads),
                         {"why": "thread", "answer": "c-answer"})
        project = dict(mention(), subject="project", task=None,
                       project={"id": "p1", "slug": "p", "team": {"identifier": "ABC"}})
        self.assertIsNone(r.research_ask(project, {}))
        self.assertEqual(r.reply_web(project), "project")
        assignment = dict(mention(), comment=None, kind="assignment")
        self.assertIsNone(r.research_ask(assignment, {}))
        self.assertEqual(r.reply_web(assignment), "off")
        self.assertEqual(r.reply_web(mention()), "handover")
        off = self.runner(UNIO_INBOX_WEB="off")
        self.assertIsNone(off.research_ask(mention(), {}))
        self.assertEqual(off.reply_web(mention()), "off")

    def test_threads_are_kept_thirty_days(self):
        r, h = self.runner(), {}
        now = inbox.parse_iso("2026-10-27T12:00:00Z")
        r.remember_thread(h, mention(root="old"), "c-old", now - timedelta(days=31))
        r.remember_thread(h, mention(root="new"), "c-new", now)
        self.assertEqual(set(h["researchThreads"]), {"new"})


class Gates(HomeCase):
    NOW = inbox.parse_iso("2026-10-27T12:00:00Z")

    def setUp(self):
        super().setUp()
        cfg = inbox.Config(dict(os.environ, UNIO_INBOX_WEB="on"), "org_test")
        self.runner = inbox.Runner(cfg)

    def gate(self, h=None, claimed=0):
        return self.runner.research_gate(h or {}, {"at": self.NOW - timedelta(seconds=claimed)}, self.NOW)

    def quota(self, window, used, resets="2026-10-27T14:00:00Z", read=None):
        return {"quota": {window: {"utilization": used, "resetsAt": resets,
                                   "at": inbox.iso(read or self.NOW - timedelta(minutes=1))}}}

    def test_a_fresh_claim_gets_thirteen_minutes(self):
        gate, no_web, left = self.gate()
        self.assertIsNone(gate)
        self.assertFalse(no_web)
        self.assertEqual(min(self.runner.cfg.research_timeout, (left - inbox.RESEARCH_MARGIN).total_seconds()), 780)

    def test_a_late_start_is_fitted_to_the_lease(self):
        # 5 minutes into the claim: 10 left, 90 s of them kept for the ending.
        gate, _, left = self.gate(claimed=300)
        self.assertIsNone(gate)
        self.assertEqual((left - inbox.RESEARCH_MARGIN).total_seconds(), 510)

    def test_under_four_minutes_left_releases_now(self):
        gate, detail, retry = self.gate(claimed=11 * 60 + 1)
        self.assertEqual((gate, retry), ("research_lease", self.NOW))
        self.assertEqual(inbox.wire_reason(gate), "other")

    def test_the_session_window_waits_for_its_reset(self):
        gate, _, retry = self.gate(self.quota("five_hour", 0.9))
        self.assertEqual(gate, "research_session_window")
        self.assertEqual(retry, inbox.parse_iso("2026-10-27T14:05:00Z"))
        self.assertEqual(inbox.wire_reason(gate), "usage_limit")
        self.assertIsNone(self.gate(self.quota("five_hour", 0.89))[0])
        stale = self.quota("five_hour", 0.95, read=self.NOW - timedelta(minutes=31))
        self.assertIsNone(self.gate(stale)[0], "a reading over 30 minutes old is unknown")

    def test_the_weekly_window_answers_without_the_web(self):
        gate, no_web, _ = self.gate(self.quota("seven_day", 0.9))
        self.assertIsNone(gate)
        self.assertTrue(no_web)

    def test_eight_research_runs_a_day(self):
        runs = [{"at": inbox.iso(self.NOW - timedelta(hours=23 - n)), "usd": 0.5, "kind": "research"}
                for n in range(8)]
        gate, _, retry = self.gate({"sessions": runs})
        self.assertEqual((gate, retry), ("research_runs", self.NOW + timedelta(hours=1)))
        self.assertIsNone(self.gate({"sessions": runs[1:]})[0])

    def test_the_reserve_keeps_forty_percent_for_replies(self):
        runs = [{"at": inbox.iso(self.NOW - timedelta(hours=20)), "usd": 5.0},
                {"at": inbox.iso(self.NOW - timedelta(hours=2)), "usd": 4.0}]
        gate, detail, retry = self.gate({"sessions": runs})
        self.assertEqual((gate, retry), ("research_reserve", self.NOW + timedelta(hours=4)))
        self.assertIn("9 USD", detail)
        runs[1]["usd"] = 3.99
        self.assertIsNone(self.gate({"sessions": runs})[0])


class Prompt(unittest.TestCase):
    def facts(self, **extra):
        return {"web": ("WebSearch", "WebFetch"), "no_web": False, "minutes": 13, "turns": 60, **extra}

    def test_research_facts(self):
        text = inbox.build_prompt(mention(), [], 1, language="pt-BR", research=self.facts())
        self.assertIn("Web research: WebSearch and WebFetch.", text)
        self.assertIn("Research limits: about 13 minutes and 60 turns.", text)
        self.assertIn("no later than your 20th web call", text)
        self.assertIn("Checkpoint marker: end your first post with this line, and remove it in your last edit: "
                      "_Pesquisa em andamento: completo esta resposta aqui em alguns minutos._", text)
        self.assertIn("Repositories: none in a research run", text)
        self.assertNotIn("research answer (comment", text)

    def test_follow_up_and_question(self):
        text = inbox.build_prompt(mention(), [], 1, research=self.facts(answer="c-9", question="ISS rates"))
        self.assertIn("This thread already has your research answer (comment c-9); start from its sources.", text)
        self.assertLess(text.index("BEGIN UNTRUSTED"), text.index("Research question, as an earlier run restated "
                                                                   "it: ISS rates"))

    def test_weekly_gate_line(self):
        text = inbox.build_prompt(mention(), [], 1, language="pt-BR", research=self.facts(web=(), no_web=True))
        self.assertIn("Web research: off this run, the weekly usage limit is nearly spent. Open your answer with "
                      '"Não pesquisei na web agora: o limite de uso desta semana está quase no fim.', text)

    def test_reply_lines(self):
        self.assertIn("write nothing and end with the research outcome line",
                      inbox.build_prompt(mention(), [], 1, web="handover"))
        project = inbox.build_prompt(dict(mention(), subject="project", task=None, project={
            "id": "p1", "slug": "p", "name": "P", "team": {"identifier": "ABC"}, "status": {}}), [], 1,
            web="project", language="pt-BR")
        self.assertIn("Pesquisa na web funciona nos comentários de tarefa: me mencione numa tarefa deste projeto.",
                      project)
        self.assertIn("Web research: not available.", inbox.build_prompt(mention(), [], 1))

    def test_localized_strings(self):
        for table in (inbox.RESEARCH_MARKER, inbox.RESEARCH_NO_WEB, inbox.RESEARCH_PROJECT):
            self.assertEqual(set(table), set(inbox.LANGUAGE_NAMES))


class Run(HomeCase):
    """Research runs end to end: main() --once, the fake Unio API and the fake claude."""

    def setUp(self):
        super().setUp()
        self.fake = FakeUnio().__enter__()
        self.addCleanup(self.fake.__exit__)
        bin_dir = self.tmp / "bin"
        write_fake_claude(bin_dir)
        self.claude_log = self.tmp / "claude.jsonl"
        os.environ.update(UNIO_API_URL=self.fake.url, FAKE_CLAUDE_LOG=str(self.claude_log), UNIO_INBOX_WEB="on",
                          PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        os.environ.pop("UNIO_INBOX_CLAUDE", None)
        self.profile("org_test", FakeUnio.RUNNER, {"ABC": []})
        self.state = Path(inbox.profile_dirs("org_test")[1])
        for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGBREAK"):
            if hasattr(signal, name):
                self.addCleanup(signal.signal, getattr(signal, name), signal.getsignal(getattr(signal, name)))

    def once(self, mode="", item=None):
        os.environ["FAKE_CLAUDE_MODE"] = mode
        self.fake.items = [item or mention()]
        with mock.patch.object(inbox, "desk_memory", return_value=None):
            return inbox.main(["--workspace", "org_test", "--once"])

    def sessions(self):
        return [json.loads(line) for line in self.claude_log.read_text(encoding="utf-8").splitlines()]

    def items(self):
        lines = (self.state / "log.jsonl").read_text(encoding="utf-8").splitlines()
        return [e for e in map(json.loads, lines) if e["event"] == "item"]

    def health(self):
        return json.loads((self.state / "health.json").read_text(encoding="utf-8"))

    def test_a_research_mention(self):
        self.assertEqual(self.once("research"), 0)
        session, = self.sessions()
        argv = session["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertTrue(argv[argv.index("--append-system-prompt-file") + 1].endswith("researcher.md"))
        self.assertIn("Research limits:", session["prompt"])
        self.assertEqual(session["headers"], ["Authorization", "X-Unio-Inbox-Claim", "X-Unio-Inbox-Item"])
        done = self.fake.seen("POST", "/v1/agents/inbox/complete")[0]["body"]
        self.assertEqual(done["outcome"], "replied")
        item, = self.items()
        self.assertEqual((item["mode"], item["research"]["why"]), ("research", "intent"))
        self.assertTrue(item["run"].endswith("-research"))
        self.assertEqual(item["web"], {"searches": 1, "fetches": 1, "sources": 1})
        self.assertEqual(item["usage"]["model"], "claude-test")
        health = self.health()
        self.assertEqual(health["researchThreads"]["c-0001"]["comment"], "c-session-reply")
        self.assertEqual(health["sessions"][-1]["kind"], "research")
        self.assertEqual(health["quota"]["seven_day"]["utilization"], 0.5)
        progress = [r["body"]["step"] for r in self.fake.seen("POST", "/v1/agents/inbox/progress")]
        self.assertNotIn("reading_code", progress)

    def test_escalation_runs_research_under_the_same_claim(self):
        self.assertEqual(self.once("escalate", mention("@[Agent](user_agent) qual a alíquota de ISS?")), 0)
        triage, research = self.sessions()
        self.assertIn("responder", triage["argv"][triage["argv"].index("--append-system-prompt-file") + 1])
        self.assertIn("Research question, as an earlier run restated it: ISS rates for software", research["prompt"])
        self.assertEqual(len(self.fake.seen("POST", "/v1/agents/inbox/claim")), 1)
        self.assertEqual(len(self.fake.seen("POST", "/v1/agents/inbox/complete")), 1)
        item, = self.items()
        self.assertEqual((item["research"]["why"], item["triage"]["turns"]), ("escalated", 3))
        self.assertEqual([s.get("kind") for s in self.health()["sessions"]], [None, "research"])

    def test_a_gate_releases_without_a_session(self):
        state = self.state
        state.mkdir(parents=True, exist_ok=True)
        (state / "health.json").write_text(json.dumps({"quota": {"five_hour": {
            "utilization": 0.97, "resetsAt": "2099-01-01T00:00:00Z", "at": inbox.iso(inbox.utcnow())}}}),
            encoding="utf-8")
        self.assertEqual(self.once("research"), 0)
        self.assertFalse(self.claude_log.exists())
        release = self.fake.seen("POST", "/v1/agents/inbox/release")[0]["body"]
        self.assertEqual(release["reason"], "usage_limit")
        self.assertNotIn("backoffUntil", {k for k, v in self.health().items() if v})

    def test_web_off_keeps_the_reply_run(self):
        os.environ["UNIO_INBOX_WEB"] = "off"
        self.assertEqual(self.once(""), 0)
        session, = self.sessions()
        self.assertIn("Web research: not available.", session["prompt"])
        self.assertTrue(session["argv"][session["argv"].index("--append-system-prompt-file") + 1]
                        .endswith("responder.md"))


class WebGuard(unittest.TestCase):
    def test_private_text(self):
        for text, kind in (("cpf 123.456.789-09", "cpf"), ("12345678909 situacao", "cpf"),
                           ("cnpj 12.345.678/0001-95", "cnpj"), ("12345678000195", "cnpj"),
                           ("processo 0001234-56.2026.8.26.0100", "case_number"),
                           ("00012345620268260100", "case_number"), ("contato ana@example.com", "email"),
                           ("ABC-12 prazo", "task_key"), ("https://x.example/?q=abc-12", "task_key"),
                           ("https://x.example/a%40b.example.com", "email")):
            self.assertEqual(inbox.private_text(text, "ABC-12"), kind, text)
        for text in ("aliquota iss software 2026", "lei 13709 2018 art 7", "https://x.example/n/1793188800123",
                     "ABC-123 is another task", "LC 116/2003", None):
            self.assertIsNone(inbox.private_text(text, "ABC-12"), text)

    def test_hosts(self):
        unio = ("api.unio.so", "unio.so")
        for url, problem in (("http://localhost:3001/", "local_name"), ("http://127.0.0.2:9/", "private_address"),
                             ("http://10.0.0.5/", "private_address"), ("http://192.168.1.1/", "private_address"),
                             ("http://172.16.0.1/", "private_address"), ("http://169.254.169.254/", "private_address"),
                             ("http://100.64.1.1/", "private_address"), ("http://[::1]/", "private_address"),
                             ("http://[::ffff:127.0.0.1]/", "private_address"), ("http://0.0.0.0/", "private_address"),
                             ("http://2130706433/", "private_address"), ("http://0x7f.1/", "private_address"),
                             ("http://127.1/", "private_address"), ("http://printer.local/", "local_name"),
                             ("http://db.internal/", "local_name"), ("http://intranet/", "local_name"),
                             ("https://api.unio.so/v1/tasks", "unio_host"), ("https://unio.so/app", "unio_host"),
                             ("https://user:pw@example.org/", "userinfo"), ("file:///etc/passwd", "scheme"),
                             ("ftp://example.org/", "scheme")):
            self.assertEqual(inbox.fetch_problem(url, unio), problem, url)
        for url in ("https://www.gov.example/ccivil_03/leis/l13709.htm", "http://8.8.8.8/",
                    "https://beef.cafe.example/", "https://unio.so.example/"):
            self.assertIsNone(inbox.fetch_problem(url, unio), url)

    def checked(self, lines):
        s = inbox.Session(required=(), builtin={"WebSearch", "WebFetch"})
        s.guard = {"key": "ABC-1", "hosts": ("api.unio.so",)}
        return read(lines, s)

    def test_a_private_query_stops_the_session(self):
        s = self.checked([*use(1, "WebSearch", {"query": "ABC-1 prazo"}),
                          *use(2, "WebSearch", {"query": "ana@example.com"})])
        self.assertEqual(s.violation, "WebSearch task_key")
        self.assertEqual(s.proc.stopped, 1)

    def test_a_private_fetch_stops_it(self):
        s = self.checked(use(1, "WebFetch", {"url": "http://127.0.0.2:9/"}))
        self.assertEqual(s.violation, "WebFetch private_address")

    def test_a_redirect_to_a_private_host_stops_it(self):
        s = self.checked(fetch(1, "https://short.example/x", 200, "http://192.168.0.1/admin"))
        self.assertEqual(s.violation, "WebFetch private_address")

    def test_public_calls_pass(self):
        s = self.checked([*use(1, "WebSearch", {"query": "aliquota iss software"}),
                          *fetch(2, "https://www.gov.example/a")])
        self.assertIsNone(s.violation)
        self.assertEqual(s.proc.stopped, 0)

    def test_a_violation_ends_like_a_cap(self):
        r = inbox.Runner.__new__(inbox.Runner)
        s = inbox.Session()
        s.result, s.returncode, s.violation = {"subtype": "success"}, 0, "WebSearch cpf"
        v = r._ended(s, inbox.utcnow(), 780)
        self.assertEqual(v.kind, "too_large")
        self.assertIn("WebSearch cpf", v.error)


class DenyRules(HomeCase):
    def test_fetch_deny_rules(self):
        self.profile("org_test")
        r = inbox.Runner(inbox.Config({**os.environ, "UNIO_INBOX_WEB": "on"}, "org_test"))
        denied = r.research_args(Path("/m.json"), ("WebSearch", "WebFetch"))
        denied = denied[denied.index("--disallowedTools") + 1].split(",")
        for host in ("localhost", "127.0.0.1", "0.0.0.0", "api.unio.so", "unio.so"):
            self.assertIn(f"WebFetch(domain:{host})", denied)
        r._workspace = {"appUrl": "https://app.example.org"}
        self.assertEqual(r.unio_hosts(), ("api.unio.so", "app.example.org"))
        search = r.research_args(Path("/m.json"), ("WebSearch",))
        self.assertNotIn("WebFetch(", search[search.index("--disallowedTools") + 1])
