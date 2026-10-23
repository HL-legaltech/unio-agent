"""Where the runner keeps its files: the single-machine files as they have
always been, one profile per workspace on each system, and the env file."""
import json
import os
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

from inbox_support import HomeCase, inbox


class LegacyPaths(HomeCase):
    """Without --workspace nothing moves: a machine set up before profiles
    keeps working when it runs this file."""

    def test_default_folders(self):
        cfg = inbox.Config(dict(os.environ))
        self.assertIsNone(cfg.workspace)
        self.assertEqual(cfg.config_dir, self.home / ".config" / "unio")
        self.assertEqual(cfg.state_dir, self.home / ".local" / "state" / "unio" / "inbox")
        self.assertEqual(cfg.token_file, self.home / ".config" / "unio" / "token")
        self.assertEqual(cfg.responder, self.home / ".config" / "unio" / "responder.md")
        self.assertEqual(cfg.reviewer, self.home / ".config" / "unio" / "reviewer.md")
        self.assertEqual(cfg.needs_protocol, self.home / ".config" / "unio" / "needs.md")
        self.assertEqual(cfg.repos_file, self.home / ".config" / "unio" / "repos.json")
        self.assertEqual(cfg.runner_id_file, self.home / ".config" / "unio" / "runner-id")
        self.assertEqual(cfg.token_hint, "run unio-auth")

    def test_runner_token_goes_first(self):
        folder = self.home / ".config" / "unio"
        folder.mkdir(parents=True)
        (folder / "runner-token").write_text("x", encoding="utf-8")
        self.assertEqual(inbox.Config(dict(os.environ)).token_file, folder / "runner-token")

    def test_xdg_folders(self):
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.tmp / "c"), XDG_STATE_HOME=str(self.tmp / "s"))
        cfg = inbox.Config(env)
        self.assertEqual(cfg.config_dir, self.tmp / "c" / "unio")
        self.assertEqual(cfg.state_dir, self.tmp / "s" / "unio" / "inbox")

    def test_protocols_are_never_the_shipped_ones(self):
        cfg = inbox.Config(dict(os.environ))
        self.assertFalse(cfg.responder.is_file())
        self.assertNotEqual(cfg.responder.parent, inbox.PROTOCOL_DIR)


class ProfilePaths(HomeCase):
    def test_linux_and_macos(self):
        for platform in ("linux", "darwin"):
            config, state = inbox.profile_dirs("org_abc", {}, platform)
            self.assertEqual(config, PurePosixPath(self.home, ".config/unio/runners/org_abc"))
            self.assertEqual(state, PurePosixPath(self.home, ".local/state/unio/runners/org_abc"))
            config, state = inbox.profile_dirs("org_abc", {"XDG_CONFIG_HOME": "/x/c", "XDG_STATE_HOME": "/x/s"},
                                               platform)
            self.assertEqual(config, PurePosixPath("/x/c/unio/runners/org_abc"))
            self.assertEqual(state, PurePosixPath("/x/s/unio/runners/org_abc"))

    def test_windows(self):
        env = {"APPDATA": r"C:\Users\u\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\u\AppData\Local",
               "XDG_CONFIG_HOME": "/ignored"}
        config, state = inbox.profile_dirs("org_abc", env, "win32")
        self.assertEqual(str(config), r"C:\Users\u\AppData\Roaming\unio\runners\org_abc")
        self.assertEqual(str(state), r"C:\Users\u\AppData\Local\unio\runners\org_abc")
        config, state = inbox.profile_dirs("org_abc", {"USERPROFILE": r"D:\home\u"}, "win32")
        self.assertEqual(config, PureWindowsPath(r"D:\home\u\AppData\Roaming\unio\runners\org_abc"))
        self.assertEqual(state, PureWindowsPath(r"D:\home\u\AppData\Local\unio\runners\org_abc"))

    def test_patched_platform_reaches_the_rule(self):
        with mock.patch.object(inbox.sys, "platform", "win32"):
            config, _ = inbox.profile_dirs("org_abc", {"APPDATA": r"C:\A"})
        self.assertEqual(str(config), r"C:\A\unio\runners\org_abc")

    def test_profile_config(self):
        legacy = self.home / ".config" / "unio"
        legacy.mkdir(parents=True)
        (legacy / "token").write_text("person", encoding="utf-8")
        (legacy / "responder.md").write_text("old", encoding="utf-8")
        cfg = inbox.Config(dict(os.environ), "org_abc")
        config, state = (Path(p) for p in inbox.profile_dirs("org_abc"))
        self.assertEqual((cfg.config_dir, cfg.state_dir), (config, state))
        self.assertEqual(cfg.token_file, config / "runner-token")
        self.assertEqual(cfg.responder, inbox.PROTOCOL_DIR / "responder.md")
        self.assertEqual(cfg.reviewer, inbox.PROTOCOL_DIR / "reviewer.md")
        self.assertEqual(cfg.needs_protocol, inbox.PROTOCOL_DIR / "needs.md")
        self.assertTrue(cfg.responder.is_file())
        self.assertIn("org_abc", cfg.token_hint)

    def test_profile_override_wins(self):
        config = Path(inbox.profile_dirs("org_abc")[0])
        config.mkdir(parents=True)
        (config / "reviewer.md").write_text("mine", encoding="utf-8")
        cfg = inbox.Config(dict(os.environ), "org_abc")
        self.assertEqual(cfg.reviewer, config / "reviewer.md")
        self.assertEqual(cfg.responder, inbox.PROTOCOL_DIR / "responder.md")


class EnvFile(HomeCase):
    def write(self, text):
        path = self.tmp / "env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_parse(self):
        path = self.write("\ufeff# a comment\n\nUNIO_INBOX_MODEL=sonnet\n  UNIO_REVIEW = observe \n"
                          "QUOTED=\"a b\"\nSINGLE='c'\nEMPTY=\nnot a line\n1BAD=x\nWITH=eq=sign\n")
        self.assertEqual(inbox.read_env_file(path), {"UNIO_INBOX_MODEL": "sonnet", "UNIO_REVIEW": "observe",
                                                     "QUOTED": "a b", "SINGLE": "c", "EMPTY": "",
                                                     "WITH": "eq=sign"})

    def test_missing_file(self):
        self.assertEqual(inbox.read_env_file(self.tmp / "none"), {})

    def test_real_environment_wins(self):
        path = self.write("UNIO_INBOX_MODEL=from-file\nUNIO_INBOX_WEB=search\nUNIO_INBOX_TIMEOUT=\n")
        environ = {"UNIO_INBOX_MODEL": "from-env", "UNIO_INBOX_TIMEOUT": ""}
        self.assertEqual(inbox.apply_env_file(path, environ), ["UNIO_INBOX_WEB"])
        self.assertEqual(environ, {"UNIO_INBOX_MODEL": "from-env", "UNIO_INBOX_WEB": "search",
                                   "UNIO_INBOX_TIMEOUT": ""})

    def test_cannot_move_the_profile(self):
        path = self.write("XDG_CONFIG_HOME=/elsewhere\nHOME=/elsewhere\nAPPDATA=x\nUNIO_INBOX_WEB=on\n")
        self.assertEqual(inbox.read_env_file(path), {"UNIO_INBOX_WEB": "on"})

    def test_main_reads_it_before_the_config(self):
        config = Path(inbox.profile_dirs("org_abc")[0])
        config.mkdir(parents=True)
        (config / "env").write_text("UNIO_INBOX_INTERVAL=7\n", encoding="utf-8")
        seen = {}

        class Stop(Exception):
            pass

        def fake_runner(cfg):
            seen.update(interval=cfg.interval, keys=cfg.env_keys)
            raise Stop

        os.environ.pop("UNIO_INBOX_INTERVAL", None)
        with mock.patch.object(inbox, "Runner", fake_runner), self.assertRaises(Stop):
            inbox.main(["--workspace", "org_abc", "--dry-run"])
        self.assertEqual(seen, {"interval": 7.0, "keys": ["UNIO_INBOX_INTERVAL"]})


class CommandLine(HomeCase):
    def test_print_paths(self):
        p = self.run_inbox("--workspace", "org_abc", "--print-paths")
        self.assertEqual(p.returncode, 0, p.stderr)
        config, state = (str(Path(x)) for x in inbox.profile_dirs("org_abc"))
        self.assertEqual(json.loads(p.stdout), {"config": config, "state": state,
                                                "token": str(Path(config) / "runner-token")})
        self.assertFalse(Path(config).exists(), "--print-paths must create nothing")

    def test_print_paths_without_a_profile(self):
        p = self.run_inbox("--print-paths")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["state"], str(self.home / ".local" / "state" / "unio" / "inbox"))

    def test_workspace_id_is_checked(self):
        for bad in ("org_", "org-abc", "abc", "org_a/b", "org_a b"):
            p = self.run_inbox("--workspace", bad, "--print-paths")
            self.assertEqual(p.returncode, 2, bad)
            self.assertIn("--workspace", p.stderr)


if __name__ == "__main__":
    unittest.main()
