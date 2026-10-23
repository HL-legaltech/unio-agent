"""A profile's self-update: the version rule and the decision, against a
local bare repository standing in for the repository's origin."""
import os
import shutil
import subprocess
import unittest
from datetime import timedelta
from unittest import mock

from inbox_support import FakeUnio, HomeCase, inbox


class Versions(unittest.TestCase):
    def test_compare(self):
        order = ["1.7", "2.0.0", "2.0.1", "2.1.0", "10.0.0"]
        for lower, higher in zip(order, order[1:]):
            self.assertTrue(inbox.newer(higher, lower), (higher, lower))
            self.assertFalse(inbox.newer(lower, higher), (lower, higher))
        self.assertFalse(inbox.newer("2.0", "2.0.0"))
        self.assertFalse(inbox.newer("2.0.0\n", "2.0.0"))

    def test_unreadable_never_wins(self):
        for bad in (None, "", "v2.1.0", "2.1.0-rc1", "2.x", "next"):
            self.assertFalse(inbox.newer(bad, "2.0.0"), bad)
            self.assertFalse(inbox.newer("9.0.0", bad), bad)

    def test_switch(self):
        self.assertTrue(inbox.autoupdate_on({}))
        self.assertTrue(inbox.autoupdate_on({"UNIO_RUNNER_AUTOUPDATE": "on"}))
        for off in ("off", "OFF", " off ", "0", "false", "no"):
            self.assertFalse(inbox.autoupdate_on({"UNIO_RUNNER_AUTOUPDATE": off}), off)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Decision(HomeCase):
    def setUp(self):
        super().setUp()
        # The home is the test's own, so no global git configuration (signing, hooks) applies.
        os.environ.update(GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Runner Test", GIT_AUTHOR_EMAIL="runner-test",
                          GIT_COMMITTER_NAME="Runner Test", GIT_COMMITTER_EMAIL="runner-test")
        self.origin = self.tmp / "origin.git"
        self.seed = self.tmp / "seed"
        self.clone = self.tmp / "clone"
        self.git("init", "--quiet", "--bare", str(self.origin), cwd=self.tmp)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.origin)
        self.git("init", "--quiet", str(self.seed), cwd=self.tmp)
        self.git("checkout", "--quiet", "-b", "main", cwd=self.seed)
        self.publish("2.0.0")
        self.git("clone", "--quiet", str(self.origin), str(self.clone), cwd=self.tmp)
        self.events = []
        self.now = inbox.utcnow()

    def git(self, *args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)

    def publish(self, version):
        (self.seed / "runner").mkdir(exist_ok=True)
        (self.seed / "runner" / "VERSION").write_text(version + "\n", encoding="utf-8")
        (self.seed / "runner" / "unio-inbox").write_text(f"# runner {version}\n", encoding="utf-8")
        self.git("add", "runner", cwd=self.seed)
        self.git("commit", "--quiet", "-m", f"runner {version}", cwd=self.seed)
        self.git("push", "--quiet", str(self.origin), "main", cwd=self.seed)

    def updater(self, version="2.0.0", root=None):
        return inbox.Updater(root or self.clone, "git", lambda event, **f: self.events.append((event, f)), version)

    def head_version(self):
        return (self.clone / "runner" / "VERSION").read_text(encoding="utf-8").strip()

    def test_nothing_newer(self):
        u = self.updater()
        self.assertIsNone(u.check(self.now))
        self.assertEqual(self.events, [])
        self.assertFalse(u.due(self.now + timedelta(hours=5)))
        self.assertTrue(u.due(self.now + timedelta(hours=6)))

    def test_newer_main_fast_forwards(self):
        self.publish("2.1.0")
        self.assertEqual(self.updater().check(self.now), ("2.0.0", "2.1.0"))
        self.assertEqual(self.head_version(), "2.1.0")
        self.assertEqual(self.events, [])

    def test_local_changes_hold_it(self):
        self.publish("2.1.0")
        (self.clone / "runner" / "unio-inbox").write_text("# edited\n", encoding="utf-8")
        self.assertIsNone(self.updater().check(self.now))
        self.assertEqual(self.head_version(), "2.0.0")
        self.assertEqual([e for e, _ in self.events], ["update_skipped"])
        self.assertEqual(self.events[0][1]["reason"], "local_changes")

    def test_another_branch_holds_it(self):
        self.publish("2.1.0")
        self.git("checkout", "--quiet", "-b", "mine", cwd=self.clone)
        self.assertIsNone(self.updater().check(self.now))
        self.assertEqual(self.events[0][1]["reason"], "branch")

    def test_local_commits_hold_it(self):
        self.publish("2.1.0")
        (self.clone / "extra").write_text("x\n", encoding="utf-8")
        self.git("add", "extra", cwd=self.clone)
        self.git("commit", "--quiet", "-m", "local", cwd=self.clone)
        self.assertIsNone(self.updater().check(self.now))
        self.assertEqual(self.events[0][1]["reason"], "diverged")
        self.assertEqual(self.head_version(), "2.0.0")

    def test_a_clone_already_moved_restarts_without_git(self):
        self.git("remote", "set-url", "origin", str(self.tmp / "gone.git"), cwd=self.clone)
        u = self.updater(version="1.9.0")
        self.assertTrue(u.due(self.now))
        self.assertEqual(u.check(self.now), ("1.9.0", "2.0.0"))

    def test_fetch_failure_waits_for_the_next_check(self):
        self.git("remote", "set-url", "origin", str(self.tmp / "gone.git"), cwd=self.clone)
        u = self.updater()
        self.assertIsNone(u.check(self.now))
        self.assertEqual([e for e, _ in self.events], ["update_failed"])
        self.assertFalse(u.due(self.now + timedelta(minutes=1)))

    def test_not_a_clone(self):
        plain = self.tmp / "plain"
        (plain / "runner").mkdir(parents=True)
        (plain / "runner" / "VERSION").write_text("2.0.0\n", encoding="utf-8")
        self.assertIsNone(self.updater(root=plain).check(self.now))
        self.assertEqual(self.events[0][1]["reason"], "not_a_clone")


class Serve(HomeCase):
    def test_an_update_ends_the_service_for_a_restart(self):
        with FakeUnio() as fake:
            os.environ["UNIO_API_URL"] = fake.url
            runner = inbox.Runner(inbox.Config(dict(os.environ), "org_test"))
            updater = mock.Mock()
            updater.due.return_value = True
            updater.check.return_value = ("2.0.0", "2.1.0")
            with mock.patch.object(runner, "locked_poll") as poll:
                self.assertEqual(runner.serve(updater), inbox.RESTART_EXIT)
            poll.assert_not_called()
            log = runner.state.log_file.read_text(encoding="utf-8")
            self.assertIn('"event": "updated", "from": "2.0.0", "to": "2.1.0"', log)
            self.assertIn('"exit": 75', log)
            self.assertIn('"autoUpdate": "on"', log)
            # Presence goes out only when the runner says something: an update says nothing.
            self.assertEqual(fake.seen("PUT"), [])


if __name__ == "__main__":
    unittest.main()
