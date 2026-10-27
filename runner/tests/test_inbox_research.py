"""Research on mention: the steps web tools report, the stream the runner
reads, and the research profile's limits, gates and endings."""
import unittest

from inbox_support import inbox


class Steps(unittest.TestCase):
    def test_web_tools_report_researching(self):
        # The server has known this code since the coding-session watcher (DEV-439).
        self.assertEqual(inbox.STEPS["WebSearch"], "researching")
        self.assertEqual(inbox.STEPS["WebFetch"], "researching")
