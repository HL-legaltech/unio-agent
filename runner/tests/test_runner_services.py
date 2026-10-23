"""The service definitions unio-runner writes and the commands it runs on each
system, with sys.platform patched, so every branch runs on every OS."""

import os
import plistlib
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest import mock

from runner_support import Recorder, load_runner

ur = load_runner()
TASK = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def lines(text):
    return text.splitlines()


class Kind(unittest.TestCase):
    def test_each_system_gets_its_service_manager(self):
        for platform, kind, cls in (("linux", "systemd", ur.Systemd), ("darwin", "launchd", ur.Launchd),
                                    ("win32", "schtasks", ur.Tasks)):
            with mock.patch.object(sys, "platform", platform):
                self.assertEqual(ur.service_kind(), kind)
                self.assertIsInstance(ur.service(run_command=Recorder()), cls)

    def test_other_systems_get_none(self):
        for platform in ("freebsd14", "cygwin", "msys", "aix"):
            with mock.patch.object(sys, "platform", platform):
                self.assertIsNone(ur.service_kind())
                self.assertIsNone(ur.service())

    def test_names(self):
        self.assertEqual(ur.unit_name("org_x"), "unio-runner@org_x.service")
        self.assertEqual(ur.launchd_label("org_x"), "so.unio.runner.org_x")
        self.assertEqual(ur.task_name("org_x"), "Unio Runner org_x")


class SystemdUnit(unittest.TestCase):
    def test_unit_runs_the_runner_per_instance(self):
        text = ur.render_systemd("/opt/py 3/bin/python3", "/home/someone/agent/runner/unio-inbox",
                                 "/home/someone/.local/bin:/usr/bin:/bin")
        for line in ('ExecStart="/opt/py 3/bin/python3" "/home/someone/agent/runner/unio-inbox" --workspace %i',
                     "Restart=always", "RestartSec=10", 'Environment="PATH=/home/someone/.local/bin:/usr/bin:/bin"',
                     "Environment=PYTHONUTF8=1", "MemoryHigh=768M", "MemoryMax=1G", "KillMode=mixed",
                     "TimeoutStopSec=120", "WantedBy=default.target", "Type=simple"):
            self.assertIn(line, lines(text))
        self.assertNotIn("${", text)

    def test_values_are_escaped_for_systemd(self):
        text = ur.render_systemd('/p/100%/py"t\\$HOME', "/i", "/a$b:/c%d")
        self.assertIn('ExecStart="/p/100%%/py\\"t\\\\$$HOME" "/i" --workspace %i', lines(text))
        # Environment= expands specifiers but never $.
        self.assertIn('Environment="PATH=/a$b:/c%%d"', lines(text))

    def test_a_line_break_cannot_add_a_directive(self):
        with self.assertRaises(ValueError):
            ur.render_systemd("/usr/bin/python3\nExecStartPre=/bin/false", "/i", "/usr/bin")
        with self.assertRaises(ValueError):
            ur.render_systemd("/usr/bin/python3", "/i", "/usr/bin\rx")


class LaunchdPlist(unittest.TestCase):
    def test_plist_keeps_the_runner_alive(self):
        text = ur.render_launchd("org_x", "/opt/homebrew/bin/python3", "/Users/a b/agent/runner/unio-inbox",
                                 "/opt/homebrew/bin:/usr/bin:/bin", "/Users/a b/state/service.log")
        data = plistlib.loads(text.encode("utf-8"))
        self.assertEqual(data["Label"], "so.unio.runner.org_x")
        self.assertEqual(data["ProgramArguments"], ["/opt/homebrew/bin/python3", "/Users/a b/agent/runner/unio-inbox",
                                                    "--workspace", "org_x"])
        self.assertIs(data["RunAtLoad"], True)
        self.assertIs(data["KeepAlive"], True)
        self.assertEqual(data["ThrottleInterval"], 10)
        self.assertEqual(data["ExitTimeOut"], 120)
        self.assertEqual(data["EnvironmentVariables"]["PATH"], "/opt/homebrew/bin:/usr/bin:/bin")
        self.assertEqual(data["EnvironmentVariables"]["PYTHONUTF8"], "1")
        self.assertEqual(data["StandardOutPath"], "/Users/a b/state/service.log")
        self.assertEqual(data["StandardErrorPath"], "/Users/a b/state/service.log")
        self.assertNotIn("${", text)

    def test_xml_characters_survive(self):
        text = ur.render_launchd("org_x", "/p/<py>&", "/i", "/a&b", "/l")
        data = plistlib.loads(text.encode("utf-8"))
        self.assertEqual(data["ProgramArguments"][0], "/p/<py>&")
        self.assertEqual(data["EnvironmentVariables"]["PATH"], "/a&b")


class TaskXml(unittest.TestCase):
    def render(self, **kw):
        args = dict(org="org_x", pythonw="C:\\Python312\\pythonw.exe",
                    inbox="C:\\Users\\a b\\AppData\\Local\\unio\\agent\\runner\\unio-inbox",
                    user="DESKTOP-1\\someone", workdir="C:\\Users\\a b\\AppData\\Local\\unio\\runners\\org_x",
                    start=datetime(2026, 10, 23, 9, 41, 7, 512000))
        args.update(kw)
        text = ur.render_task(**args)
        return text, ET.fromstring(text.encode("utf-16"))

    def find(self, root, path):
        return root.find(path, TASK).text

    def test_task_runs_at_logon_and_comes_back(self):
        text, root = self.render()
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"?>'))
        self.assertEqual(self.find(root, "t:RegistrationInfo/t:URI"), "\\Unio Runner org_x")
        self.assertEqual(self.find(root, "t:Triggers/t:LogonTrigger/t:UserId"), "DESKTOP-1\\someone")
        self.assertEqual(self.find(root, "t:Triggers/t:LogonTrigger/t:Repetition/t:Interval"), "PT1M")
        # Setup starts the task with /Run, so a logon trigger's repetition would only
        # begin at the next logon: this one repeats from setup on.
        self.assertEqual(self.find(root, "t:Triggers/t:TimeTrigger/t:StartBoundary"), "2026-10-23T09:41:07")
        self.assertEqual(self.find(root, "t:Triggers/t:TimeTrigger/t:Enabled"), "true")
        self.assertEqual(self.find(root, "t:Triggers/t:TimeTrigger/t:Repetition/t:Interval"), "PT1M")
        self.assertIsNone(root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Duration", TASK))
        self.assertIsNone(root.find("t:Triggers/t:TimeTrigger/t:EndBoundary", TASK))
        self.assertEqual(self.find(root, "t:Principals/t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(self.find(root, "t:Settings/t:ExecutionTimeLimit"), "PT0S")
        self.assertEqual(self.find(root, "t:Settings/t:RestartOnFailure/t:Interval"), "PT1M")
        self.assertEqual(self.find(root, "t:Settings/t:RestartOnFailure/t:Count"), "999")
        self.assertEqual(self.find(root, "t:Settings/t:DisallowStartIfOnBatteries"), "false")
        self.assertEqual(self.find(root, "t:Settings/t:StopIfGoingOnBatteries"), "false")
        self.assertEqual(self.find(root, "t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(self.find(root, "t:Actions/t:Exec/t:Command"), "C:\\Python312\\pythonw.exe")
        self.assertEqual(self.find(root, "t:Actions/t:Exec/t:Arguments"),
                         '-X utf8 "C:\\Users\\a b\\AppData\\Local\\unio\\agent\\runner\\unio-inbox" --workspace org_x')
        self.assertEqual(self.find(root, "t:Actions/t:Exec/t:WorkingDirectory"),
                         "C:\\Users\\a b\\AppData\\Local\\unio\\runners\\org_x")
        self.assertNotIn("${", text)

    def test_a_quote_in_the_runner_path_is_refused(self):
        with self.assertRaises(ValueError):
            self.render(inbox='C:\\a"b\\unio-inbox')

    def test_user_comes_from_the_session(self):
        self.assertEqual(ur.task_user({"USERDOMAIN": "PC", "USERNAME": "someone"}), "PC\\someone")
        self.assertEqual(ur.task_user({"USERNAME": "someone"}), "someone")


class StopWait(unittest.TestCase):
    def test_outlasts_the_runners_shutdown(self):
        # unio-inbox against an API that never answers: a 20 s call under way, the
        # session's 5 s grace and 10 s for its output, the release's three 5 s
        # tries 2 s apart, and the presence's 5 s join and 3 s write.
        worst = 20 + 5 + 10 + (3 * 5 + 2 * 2) + (5 + 3)
        self.assertGreaterEqual(ur.STOP_WAIT, worst + 30)


class Interpreter(unittest.TestCase):
    def test_windows_prefers_pythonw(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "python.exe"
            exe.write_text("")
            with mock.patch.object(sys, "executable", str(exe)):
                self.assertEqual(ur.service_python("schtasks"), str(exe))
                (Path(tmp) / "pythonw.exe").write_text("")
                self.assertEqual(ur.service_python("schtasks"), str(Path(tmp) / "pythonw.exe"))
                self.assertEqual(ur.service_python("systemd"), str(exe))
                self.assertEqual(ur.service_python("launchd"), str(exe))

    def test_service_path_keeps_absolute_folders_once(self):
        first, second = os.path.abspath("one"), os.path.abspath("two")
        path = os.pathsep.join([first, "", "relative", second, first])
        self.assertEqual(ur.service_path({"PATH": path}), os.pathsep.join([first, second]))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.prof = ur.Profile("org_x", Path(tmp.name) / "config", Path(tmp.name) / "state",
                               Path(tmp.name) / "config" / "runner-token")
        self.prof.state.mkdir()

    def assertNeverTheLiveService(self, rec):
        for argv in rec.calls:
            self.assertNotIn("unio-inbox.service", " ".join(argv))


class SystemdCommands(Base):
    def test_install_writes_the_template_and_starts_the_instance(self):
        rec = Recorder()
        svc = ur.Systemd(home=self.home, run_command=rec)
        svc.install("org_x", self.prof, "/usr/bin/python3", "/c/runner/unio-inbox", "/usr/bin:/bin")
        unit = self.home / ".config" / "systemd" / "user" / "unio-runner@.service"
        self.assertIn('ExecStart="/usr/bin/python3" "/c/runner/unio-inbox" --workspace %i',
                      unit.read_text(encoding="utf-8").splitlines())
        self.assertEqual(rec.calls, [["systemctl", "--user", "daemon-reload"],
                                     ["systemctl", "--user", "enable", "unio-runner@org_x.service"],
                                     ["systemctl", "--user", "restart", "unio-runner@org_x.service"]])
        self.assertNeverTheLiveService(rec)

    def test_a_failed_step_stops_with_its_reason(self):
        rec = Recorder({("systemctl", "--user", "restart"): (1, "", "Job for unio-runner@org_x.service failed.\n")})
        with self.assertRaises(ur.Stop) as caught:
            ur.Systemd(home=self.home, run_command=rec).install("org_x", self.prof, "/p", "/i", "/usr/bin")
        self.assertIn("Não foi possível ligar unio-runner@org_x.service (Job for unio-runner@org_x.service failed.)",
                      caught.exception.message)

    def test_start_stop_restart(self):
        rec = Recorder()
        svc = ur.Systemd(home=self.home, run_command=rec)
        svc.start("org_x")
        svc.stop("org_x")
        svc.restart("org_x")
        self.assertEqual(rec.calls, [["systemctl", "--user", "enable", "--now", "unio-runner@org_x.service"],
                                     ["systemctl", "--user", "disable", "--now", "unio-runner@org_x.service"],
                                     ["systemctl", "--user", "restart", "unio-runner@org_x.service"]])

    def test_remove_drops_the_template_only_for_the_last_workspace(self):
        rec = Recorder()
        svc = ur.Systemd(home=self.home, run_command=rec)
        svc.install("org_x", self.prof, "/p", "/i", "/usr/bin")
        svc.remove("org_x", last=False)
        self.assertTrue(svc.unit_file.exists())
        svc.remove("org_x", last=True)
        self.assertFalse(svc.unit_file.exists())
        self.assertEqual(rec.calls[-1], ["systemctl", "--user", "daemon-reload"])
        self.assertIn(["systemctl", "--user", "disable", "--now", "unio-runner@org_x.service"], rec.calls)
        self.assertNeverTheLiveService(rec)

    def test_state(self):
        svc = ur.Systemd(home=self.home, run_command=Recorder())
        self.assertEqual(svc.state("org_x"), "absent")
        svc.install("org_x", self.prof, "/p", "/i", "/usr/bin")
        for word, state in (("active", "running"), ("activating", "running"), ("inactive", "stopped"),
                            ("failed", "failed"), ("", "unknown")):
            svc.run = Recorder({("systemctl", "--user", "is-active"): (0 if word == "active" else 3, word + "\n", "")})
            self.assertEqual(svc.state("org_x"), state, word)

    def test_linger_hint(self):
        svc = ur.Systemd(home=self.home, run_command=Recorder({("loginctl",): (0, "no\n", "")}))
        hints = svc.hints("someone")
        self.assertEqual(len(hints), 1)
        self.assertIn("loginctl enable-linger someone", hints[0])
        self.assertEqual(svc.run.calls, [["loginctl", "show-user", "someone", "--property=Linger", "--value"]])
        svc.run = Recorder({("loginctl",): (0, "yes\n", "")})
        self.assertEqual(svc.hints("someone"), [])


class LaunchdCommands(Base):
    def svc(self, rec):
        return ur.Launchd(home=self.home, run_command=rec, uid=501)

    def test_install_lints_then_loads_the_agent(self):
        rec = Recorder()
        self.svc(rec).install("org_x", self.prof, "/usr/bin/python3", "/c/runner/unio-inbox", "/usr/bin:/bin")
        plist = self.home / "Library" / "LaunchAgents" / "so.unio.runner.org_x.plist"
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["StandardOutPath"], str(self.prof.state / "service.log"))
        self.assertEqual(rec.calls, [["plutil", "-lint", str(plist)],
                                     ["launchctl", "bootout", "gui/501/so.unio.runner.org_x"],
                                     ["launchctl", "enable", "gui/501/so.unio.runner.org_x"],
                                     ["launchctl", "bootstrap", "gui/501", str(plist)]])

    def test_a_refused_bootstrap_says_where_to_run_it(self):
        rec = Recorder({("launchctl", "bootstrap"): (5, "", "Bootstrap failed: 125: Domain does not support specified action\n")})
        with self.assertRaises(ur.Stop) as caught:
            self.svc(rec).install("org_x", self.prof, "/p", "/i", "/usr/bin")
        self.assertIn("Bootstrap failed: 125", caught.exception.message)
        self.assertIn("Terminal do próprio Mac", caught.exception.message)

    def test_a_bad_plist_is_never_loaded(self):
        rec = Recorder({("plutil",): (1, "file.plist: Encountered unexpected character\n", "")})
        with self.assertRaises(ur.Stop):
            self.svc(rec).install("org_x", self.prof, "/p", "/i", "/usr/bin")
        self.assertFalse(any(argv[0] == "launchctl" for argv in rec.calls))

    def test_start_stop_restart_remove(self):
        rec = Recorder({("launchctl", "bootstrap"): (37, "", "Bootstrap failed: 37: Operation already in progress\n")})
        svc = self.svc(rec)
        target ="gui/501/so.unio.runner.org_x"
        plist = str(svc.plist("org_x"))
        svc.start("org_x")
        self.assertEqual(rec.calls, [["launchctl", "enable", target], ["launchctl", "bootstrap", "gui/501", plist],
                                     ["launchctl", "kickstart", target]])
        rec.calls.clear()
        svc.stop("org_x")
        svc.restart("org_x")
        self.assertEqual(rec.calls, [["launchctl", "disable", target], ["launchctl", "bootout", target],
                                     ["launchctl", "kickstart", "-k", target]])
        svc.plist("org_x").parent.mkdir(parents=True)
        svc.plist("org_x").write_text("")
        svc.remove("org_x", last=True)
        self.assertFalse(svc.plist("org_x").exists())

    def test_state(self):
        rec = Recorder()
        svc = self.svc(rec)
        self.assertEqual(svc.state("org_x"), "absent")
        svc.plist("org_x").parent.mkdir(parents=True)
        svc.plist("org_x").write_text("")
        svc.run = Recorder({("launchctl", "print"): (113, "", "Could not find service")})
        self.assertEqual(svc.state("org_x"), "stopped")
        svc.run = Recorder({("launchctl", "print"): (0, "gui/501/so.unio.runner.org_x = {\n\tstate = running\n}", "")})
        self.assertEqual(svc.state("org_x"), "running")
        svc.run = Recorder({("launchctl", "print"): (0, "x = {\n\tstate = not running\n}", "")})
        self.assertEqual(svc.state("org_x"), "stopped")


class TaskCommands(Base):
    NAME = "Unio Runner org_x"

    def test_install_creates_from_utf16_xml_and_runs(self):
        rec = Recorder()
        ur.Tasks(run_command=rec, user="PC\\someone").install("org_x", self.prof, "C:\\py\\pythonw.exe",
                                                               "C:\\a\\runner\\unio-inbox", "")
        xml = self.prof.state / "task.xml"
        self.assertTrue(xml.read_bytes().startswith(b"\xff\xfe"))
        root = ET.fromstring(xml.read_bytes())
        self.assertEqual(root.find("t:Triggers/t:LogonTrigger/t:UserId", TASK).text, "PC\\someone")
        start = datetime.fromisoformat(root.find("t:Triggers/t:TimeTrigger/t:StartBoundary", TASK).text)
        self.assertLess(abs((datetime.now() - start).total_seconds()), 60)
        self.assertEqual(rec.calls, [["schtasks", "/Create", "/TN", self.NAME, "/XML", str(xml), "/F"],
                                     ["schtasks", "/Run", "/TN", self.NAME]])

    def test_start_stop_restart_remove(self):
        rec = Recorder()
        svc = ur.Tasks(run_command=rec, user="PC\\someone")
        svc.start("org_x")
        svc.stop("org_x")
        svc.restart("org_x")
        svc.remove("org_x", last=True)
        self.assertEqual(rec.calls, [
            ["schtasks", "/Change", "/TN", self.NAME, "/ENABLE"], ["schtasks", "/Run", "/TN", self.NAME],
            ["schtasks", "/Change", "/TN", self.NAME, "/DISABLE"], ["schtasks", "/End", "/TN", self.NAME],
            ["schtasks", "/End", "/TN", self.NAME], ["schtasks", "/Run", "/TN", self.NAME],
            ["schtasks", "/End", "/TN", self.NAME], ["schtasks", "/Delete", "/TN", self.NAME, "/F"]])

    def test_state_reads_the_number_not_the_localized_word(self):
        svc = ur.Tasks(run_command=Recorder())
        for number, state in (("4", "running"), ("3", "stopped"), ("1", "stopped"), ("-1", "absent")):
            svc.run = Recorder({("powershell",): (0, number + "\r\n", "")})
            self.assertEqual(svc.state("org_x"), state, number)
            # At the root, where a standard user may register a task.
            self.assertIn("-TaskPath '\\' -TaskName 'Unio Runner org_x'", svc.run.calls[0][-1])
        svc.run = Recorder({("powershell",): (127, "", "not found"), ("schtasks", "/Query"): (0, "", "")})
        self.assertEqual(svc.state("org_x"), "unknown")
        svc.run = Recorder({("powershell",): (127, "", "not found"), ("schtasks", "/Query"): (1, "", "ERROR")})
        self.assertEqual(svc.state("org_x"), "absent")


if __name__ == "__main__":
    unittest.main()
