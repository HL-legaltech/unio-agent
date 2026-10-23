"""The agent's writing language and the workspace section the runner writes
into the responder protocol."""
import os
import unittest
from pathlib import Path

from inbox_support import HomeCase, inbox


def item(kind="mention"):
    return {
        "key": "comment:c1", "kind": kind, "subject": "task", "attempts": 0,
        "task": {"id": "t1", "key": "ABC-1", "url": "https://unio.so/app/tasks/ABC-1", "title": "A title",
                 "team": {"name": "Team", "identifier": "ABC"}, "status": {"name": "Todo", "category": "unstarted"}},
        "comment": {"id": "c1", "content": "@bot hello", "author": {"kind": "user", "name": "Someone"}},
    }


class Language(unittest.TestCase):
    def test_selection(self):
        self.assertEqual(inbox.writing_language({"writing": {"language": "pt-BR"}}), "pt-BR")
        self.assertEqual(inbox.writing_language({"writing": {"language": "en"}}), "en")
        for me in ({}, {"writing": None}, {"writing": {"language": "fr"}}, {"writing": {"language": 3}}, None):
            self.assertEqual(inbox.writing_language(me), "en", me)

    def test_every_prompt_names_it(self):
        self.assertIn("Writing language: Brazilian Portuguese.",
                      inbox.build_prompt(item(), [], 1, language="pt-BR"))
        self.assertIn("Writing language: English.", inbox.build_prompt(item(), [], 1))
        self.assertIn("Writing language: Brazilian Portuguese.",
                      inbox.build_review_prompt({"id": "p1", "triggers": []}, "pt-BR"))
        view = {"key": "ABC-1", "id": "t1", "teamKey": "ABC", "status": "Todo", "statusCategory": "unstarted",
                "title": "t"}
        self.assertIn("Writing language: Brazilian Portuguese.",
                      inbox.build_needs_prompt(view, [], [], "2026-10-23", "UTC", None, "pt-BR"))

    def test_markers_stay_english(self):
        line = inbox.language_line("pt-BR")
        for marker in ("Needs <first name>", "Waiting on", "Sessions", "Needs action"):
            self.assertIn(marker, line)

    def test_fixed_replies_in_both_languages(self):
        for table in (inbox.TOO_LARGE_REPLY, inbox.PROJECT_MADE_REPLY, inbox.FINAL_REPLY, inbox.FAILURE_REASONS,
                      inbox.PROJECTS_AND):
            self.assertEqual(set(table), set(inbox.LANGUAGE_NAMES))
        self.assertEqual(set(inbox.FAILURE_REASONS["en"]), set(inbox.FAILURE_REASONS["pt-BR"]))
        self.assertIn("grande demais", inbox.TOO_LARGE_REPLY["pt-BR"])
        self.assertIn("too large", inbox.TOO_LARGE_REPLY["en"])
        reason = inbox.FAILURE_REASONS["pt-BR"]["error"]
        self.assertEqual(inbox.FINAL_REPLY["pt-BR"].format(reason=reason),
                         "**Não foi possível atender este pedido automaticamente.** "
                         "As três tentativas falharam com um erro.")

    def test_project_note(self):
        s = inbox.Session()
        self.assertIsNone(inbox.project_note(s, "pt-BR"))
        s.projects = ["[A](https://unio.so/a)", "[B](https://unio.so/b)"]
        self.assertIn("Criei [A](https://unio.so/a) e [B](https://unio.so/b)", inbox.project_note(s, "pt-BR"))
        self.assertIn("Created [A](https://unio.so/a) and [B](https://unio.so/b)", inbox.project_note(s))


class FakeApi:
    def __init__(self, me):
        self.me = me
        self.calls = 0

    def call(self, method, path, body=None, idempotency_key=None, timeout=20):
        self.calls += 1
        return 200, {"data": self.me}


class Teams(HomeCase):
    def test_agent_teams_reads_language_and_rows(self):
        runner = inbox.Runner(inbox.Config(dict(os.environ)))
        api = FakeApi({"writing": {"language": "pt-BR"}, "workspace": {"clerkId": "org_x"},
                       "teams": [{"id": 1, "name": "Team", "identifier": "ABC"}, {"name": "no id"}]})
        self.assertEqual(runner.agent_teams(api), ["Team (ABC)"])
        self.assertEqual(runner.language, "pt-BR")
        self.assertEqual(runner._team_rows, [{"id": 1, "name": "Team", "identifier": "ABC"}])
        runner.agent_teams(api)
        self.assertEqual(api.calls, 1, "teams are read again only after TEAMS_REFRESH")


class WorkspaceSection(unittest.TestCase):
    repos = {"ABC": ["acme/api", "acme/web"], "OPS": ["acme/infra"]}

    def repos_for(self, ident):
        return self.repos.get(ident, [])

    def test_teams_and_repositories(self):
        text = inbox.workspace_section([
            {"name": "Product", "identifier": "ABC", "description": "The customer app."},
            {"name": "Operations", "identifier": "OPS"},
            {"name": "Sales", "identifier": "SAL", "description": "  "},
        ], self.repos_for)
        self.assertTrue(text.startswith("## The workspace\n"))
        self.assertIn("- **Product (ABC):** The customer app. Repositories `acme/api`, `acme/web`.", text)
        self.assertIn("- **Operations (OPS):** Repository `acme/infra`.", text)
        self.assertIn("- **Sales (SAL):** No repository.", text)
        self.assertTrue(text.endswith("Check anything more specific in the repository before you state it."))

    def test_people_text_stays_on_one_line(self):
        text = inbox.workspace_section([{"name": "A\nB\x07", "identifier": "ABC", "description": "x" * 900}],
                                       self.repos_for)
        line = next(ln for ln in text.splitlines() if ln.startswith("- "))
        self.assertIn("**A B? (ABC):**", line)
        self.assertLess(len(line), 450)

    def test_no_teams(self):
        for teams in (None, [], [{"name": "no identifier"}]):
            text = inbox.workspace_section(teams, self.repos_for)
            self.assertIn("could not be read", text)
            self.assertIn("Check anything more specific", text)

    def test_fill_replaces_only_the_marker(self):
        text = "# P\r\n\r\n" + inbox.WORKSPACE_MARKER + "\r\n\r\n## Steps\r\n"
        self.assertEqual(inbox.fill_protocol(text, "## The workspace\n\nX"),
                         "# P\r\n\r\n## The workspace\n\nX\r\n\r\n## Steps\r\n")
        self.assertEqual(inbox.fill_protocol("no marker\n", "X"), "no marker\n")

    def test_shipped_responder_has_the_marker_and_no_language_rule(self):
        text = (inbox.PROTOCOL_DIR / "responder.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(inbox.WORKSPACE_MARKER), 1)
        self.assertNotIn("## The workspace", text)
        for name in ("responder.md", "reviewer.md", "needs.md"):
            shipped = (inbox.PROTOCOL_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("Write professional English", shipped)
            self.assertNotIn("Write in English", shipped)
            self.assertIn("writing language", shipped)


class ResponderFile(HomeCase):
    def test_profile_run_gets_a_filled_copy(self):
        runner = inbox.Runner(inbox.Config(dict(os.environ), "org_abc"))
        runner._team_rows = [{"name": "Product", "identifier": "ABC"}]
        out = self.tmp / "run"
        out.mkdir()
        path = runner.responder_file(out)
        self.assertEqual(path, out / "responder.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("- **Product (ABC):** No repository.", text)
        self.assertNotIn(inbox.WORKSPACE_MARKER, text)

    def test_a_text_without_the_marker_is_used_as_it_is(self):
        folder = self.home / ".config" / "unio"
        folder.mkdir(parents=True)
        (folder / "responder.md").write_text("## The workspace\n\nOur own words.\n", encoding="utf-8")
        runner = inbox.Runner(inbox.Config(dict(os.environ)))
        out = self.tmp / "run"
        out.mkdir()
        self.assertEqual(runner.responder_file(out), folder / "responder.md")
        self.assertFalse((out / "responder.md").exists())


if __name__ == "__main__":
    unittest.main()
