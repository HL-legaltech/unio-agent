"""The runner's version: one number in two places, and how it reaches Unio."""
import re
import unittest

from inbox_support import RUNNER_DIR, inbox

# The server's reading of a presence's client (agent_live_service.ts) and its length cap.
SERVER_CLIENT = re.compile(r"^unio-inbox (\d+)\.(\d+)")
CLIENT_MAX = 64


class Version(unittest.TestCase):
    def test_file_and_constant_agree(self):
        self.assertEqual((RUNNER_DIR / "VERSION").read_text(encoding="utf-8").strip(), inbox.VERSION)

    def test_semver(self):
        self.assertRegex(inbox.VERSION, r"^\d+\.\d+\.\d+$")

    def test_presence_client(self):
        for platform, name in (("linux", "linux"), ("darwin", "macos"), ("win32", "windows")):
            client = inbox.presence_client(platform)
            self.assertEqual(client, f"unio-inbox {inbox.VERSION} ({name})")
            self.assertLessEqual(len(client), CLIENT_MAX)
            major, minor = map(int, SERVER_CLIENT.match(client).groups())
            # Project comments need 1.7 or later on the server's reading.
            self.assertGreaterEqual((major, minor), (1, 7))


if __name__ == "__main__":
    unittest.main()
