"""The needs pass reading one task: which tasks it keeps in step and which it skips."""
import types
import unittest
from datetime import datetime, timezone

from inbox_support import inbox

TASK_ID = "0b6c2f4e-8d1a-4c3b-9e7f-1a2b3c4d5e6f"
DESCRIPTION = "Ship the export.\n\n## Needs Ana\n\n- [ ] Decide the file name. Done when: it is in a comment.\n"


def task(**fields):
    return {"id": TASK_ID, "name": "Export", "description": DESCRIPTION, "updatedAt": "2026-11-20T10:00:00.000Z",
            "status": "In progress", "statusCategory": "started", "taskNumber": 7,
            "team": {"identifier": "ABC"}, "labels": [], **fields}


def read(body):
    """needs_task on a task GET /tasks/<id> answers with body, in observe mode
    with checks held, so nothing but the reading itself runs."""
    runner = types.SimpleNamespace(cfg=None, needs_quiet={},
                                   needs_get=lambda api, path, timeout=20: (200, body, None))
    ctx = {"api": None, "mode": "observe", "now": datetime(2026, 11, 25, tzinfo=timezone.utc),
           "block": ("held", "checks are held"), "errors": [], "writes": 0}
    return inbox.Runner.needs_task(runner, ctx, TASK_ID, {}, None)


class NeedsTask(unittest.TestCase):
    def test_a_plain_task_is_kept_in_step(self):
        row = read(task())
        self.assertNotIn("skip", row)
        self.assertEqual((row["key"], row["open"], row["decision"]), ("ABC-7", 1, "add the label"))

    def test_a_clients_task_is_kept_in_step_like_any_other(self):
        for linked in ({"customer": {"id": "c1", "name": "Client"}}, {"isIntegrationLinked": True}):
            with self.subTest(linked=sorted(linked)):
                row = read(task(**linked))
                self.assertNotIn("skip", row)
                self.assertEqual((row["open"], row["decision"]), (1, "add the label"))

    def test_a_deleted_task_is_skipped(self):
        row = read(task(deletedAt="2026-11-21T10:00:00.000Z"))
        self.assertEqual((row["skip"], row["decision"]), (True, "skip: deleted"))


if __name__ == "__main__":
    unittest.main()
