"""The session hook's binding and presence, and the watcher it starts, against
the fake API, run as Claude Code runs them."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from support import (ENTRY, HOOK_LIMIT, LAUNCHER, SCRIPTS, WINDOWS, Transcript, base_env, kill, presence_id, processes,
                     remove_tree, wait_for)
import fake_api

POSIX_ONLY = unittest.skipIf(WINDOWS, "lists processes by their arguments, which Windows does not show")
HOST = socket.gethostname()[:64]


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-session-test-"))
        self.addCleanup(remove_tree, self.tmp)
        self.token_file = self.tmp / "token"
        self.token_file.write_text(fake_api.TOKEN + "\n")
        self.api = fake_api.FakeApi()
        self.api.start()
        self.addCleanup(self.api.stop)
        self.task = self.api.task()
        self.session = str(uuid.uuid4())
        self.presence_id = presence_id(self.session)
        self.sessions = self.tmp / "state" / "unio" / "sessions"

    def env(self, **extra):
        return base_env(self.tmp, **{"UNIO_SESSION_TOKEN_FILE": str(self.token_file),
                                     "UNIO_SESSION_API": self.api.base, **extra})

    def hook(self, event, env=None, **fields):
        data = {"session_id": self.session, "hook_event_name": event, "cwd": str(self.tmp), **fields}
        started = time.monotonic()
        p = subprocess.run([sys.executable, "-I", str(ENTRY), "hook"], input=json.dumps(data), capture_output=True,
                           text=True, env=env or self.env(), timeout=20)
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        return time.monotonic() - started, p.stdout

    def bind(self, task_id=None):
        self.sessions.mkdir(parents=True, exist_ok=True)
        task = {"id": task_id or self.task, "key": "DEV-101", "url": "https://unio.so/app/tasks/DEV-101"}
        (self.sessions / f"{self.session}.json").write_text(json.dumps({"task": task}))

    def binding(self):
        try:
            return json.loads((self.sessions / f"{self.session}.json").read_text())
        except OSError:
            return {}

    def presence(self):
        try:
            return json.loads((self.sessions / f"{self.session}.presence.json").read_text())
        except (OSError, ValueError):
            return {}

    def settled(self, limit=20):
        """Waits until the latest requested state has been sent."""
        def done():
            p = self.presence()
            return p if p.get("seq") and p.get("lastSeq") == p["seq"] else None
        return wait_for(self, done, limit, lambda: f"the presence was never sent: {self.presence()}")

    def children(self):
        """This session's detached presence senders still running."""
        return processes("send-presence", self.session)

    def assert_no_children_left(self, limit=2):
        if WINDOWS:
            return
        deadline = time.monotonic() + limit
        while self.children() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.children(), [], "a superseded sender is still waiting to send")

    def bodies(self):
        return [r["body"] for r in self.api.presence_calls()]

    def get_task_result(self, key="DEV-101"):
        payload = {"task": {"id": self.task, "key": key, "url": f"https://unio.so/app/tasks/{key}"}}
        return [{"type": "text", "text": json.dumps(payload)}]


class SessionPresence(HookCase):
    def test_session_start_reports_idle_without_waiting_on_the_network(self):
        elapsed, out = self.hook("SessionStart", source="startup")
        self.assertLess(elapsed, HOOK_LIMIT)
        self.assertEqual(out, "")
        self.settled()
        [call] = self.api.presence_calls()
        self.assertEqual(call["path"], f"/v1/agents/presence/{self.presence_id}")
        self.assertEqual(call["authorization"], f"Bearer {fake_api.TOKEN}")
        self.assertEqual(call["body"], {"kind": "session", "host": HOST, "client": "Claude Code 2.1.287",
                                        "state": "idle"})
        self.assertEqual(self.api.presences[self.presence_id]["state"], "idle")

    def test_a_prompt_in_a_bound_session_reports_its_task_once_a_minute_and_no_prompt_text(self):
        self.bind()
        self.hook("UserPromptSubmit", prompt="the secret plan for the Falcon account")
        self.settled()
        self.hook("UserPromptSubmit", prompt="and the Meridional numbers")
        time.sleep(1)
        self.assertEqual(self.bodies(), [{"kind": "session", "host": HOST, "client": "Claude Code 2.1.287",
                                          "state": "working", "taskId": self.task}])
        self.assertEqual(self.presence()["seq"], 1, "a second prompt within the minute was sent")
        raw = json.dumps([r["body"] for r in self.api.requests])
        self.assertNotIn("Falcon", raw)
        self.assertNotIn("Meridional", raw)

    def test_a_prompt_in_an_unbound_session_reports_nothing(self):
        self.hook("UserPromptSubmit", prompt="hello")
        time.sleep(1)
        self.assertEqual(self.api.presence_calls(), [])

    def test_a_unio_tool_reports_working_and_binds_the_task_first(self):
        for tool in ("mcp__plugin_unio_unio__get_task", "mcp__unio__get_task"):
            with self.subTest(tool):
                self.session = str(uuid.uuid4())
                self.api.requests.clear()
                self.hook("UserPromptSubmit", prompt="look at https://unio.so/app/tasks/DEV-101 please")
                self.hook("PostToolUse", tool_name=tool, tool_input={"task": "DEV-101"},
                          tool_response=self.get_task_result())
                self.settled()
                self.assertEqual([(b["state"], b.get("taskId")) for b in self.bodies()], [("working", self.task)])
                self.assertEqual(self.binding()["task"]["key"], "DEV-101")
                self.assertNotIn("candidate", self.binding())

    def test_follow_ups_are_listed_and_one_created_started_becomes_the_task(self):
        tool = "mcp__plugin_unio_unio__create_task"
        first = {"task": {"id": self.task, "key": "DEV-1", "url": "https://unio.so/app/tasks/DEV-1"}}
        self.hook("PostToolUse", tool_name=tool, tool_input={"name": "x"},
                  tool_response=[{"type": "text", "text": json.dumps(first)}])
        follow = {"task": {"id": str(uuid.uuid4()), "key": "DEV-2", "url": "https://unio.so/app/tasks/DEV-2"}}
        self.hook("PostToolUse", tool_name=tool, tool_input={"name": "y", "follow_up_of": "DEV-1"},
                  tool_response=[{"type": "text", "text": json.dumps(follow)}])
        self.assertEqual((self.binding()["task"]["key"], [f["key"] for f in self.binding()["follow_ups"]]),
                         ("DEV-1", ["DEV-2"]))
        started = {"task": {"id": str(uuid.uuid4()), "key": "DEV-3", "url": "https://unio.so/app/tasks/DEV-3"}}
        self.hook("PostToolUse", tool_name=tool, tool_input={"follow_up_of": "DEV-1", "status": "em andamento"},
                  tool_response=[{"type": "text", "text": json.dumps(started)}])
        self.assertEqual(self.binding()["task"]["key"], "DEV-3")
        self.assertEqual([t["key"] for t in self.binding()["earlier"]], ["DEV-1"])

    def test_no_event_puts_anything_in_claudes_context(self):
        # The hand-made hook named the bound task to Claude after a resume; the
        # plugin's never does: what someone types in Unio stays out of the context.
        self.bind()
        for event, fields in (("SessionStart", {"source": "resume"}), ("SessionStart", {"source": "compact"}),
                              ("UserPromptSubmit", {"prompt": "go"}), ("Stop", {}), ("SessionEnd", {})):
            with self.subTest(event):
                _, out = self.hook(event, **fields)
                self.assertEqual(out, "")

    def test_a_state_change_lands_at_once(self):
        # The server stores a change of state at once; holding it 11 s showed a finished turn as work.
        self.bind()
        self.hook("UserPromptSubmit", prompt="go")
        self.settled()
        elapsed, _ = self.hook("Stop")
        self.assertLess(elapsed, HOOK_LIMIT)
        self.settled(limit=20)
        writes = self.api.presence_writes
        self.assertEqual([(w["body"]["state"], w["stored"]) for w in writes], [("working", True), ("idle", True)])
        calls = self.api.presence_calls()
        self.assertLess((calls[1]["at"] - calls[0]["at"]).total_seconds(), 3 * HOOK_LIMIT)

    def test_states_land_in_the_order_they_happened(self):
        # The first write is slow to land; a later state must not be overtaken by it.
        self.api.presence_delays = [1.5]
        self.bind()
        self.hook("UserPromptSubmit", prompt="go")
        self.hook("Stop")
        self.hook("SessionEnd", reason="exit")
        self.settled(limit=20)
        self.assert_no_children_left(limit=10)
        wait_for(self, lambda: self.api.presences.get(self.presence_id, {}).get("state") == "ended", 10,
                 "the end never landed")
        states = [w["body"]["state"] for w in self.api.presence_writes]
        self.assertEqual(states[-1], "ended", states)
        self.assertEqual(states, sorted(states, key=["working", "idle", "ended"].index), states)

    def test_session_end_reports_ended_once(self):
        self.hook("SessionEnd", reason="exit")
        self.settled()
        self.assertEqual([b["state"] for b in self.bodies()], ["ended"])
        self.assertIsNotNone(self.api.presences[self.presence_id]["endedAt"])
        self.hook("SessionEnd", reason="exit")
        time.sleep(0.5)
        self.assertEqual(len(self.api.presence_calls()), 1, "ended was sent twice")

    def test_a_background_hook_that_lands_after_the_end_reports_nothing(self):
        # Stop, UserPromptSubmit and PostToolUse run with async: true, and may finish after SessionEnd.
        self.bind()
        self.hook("SessionEnd", reason="exit")
        self.settled()
        self.hook("Stop")
        self.hook("UserPromptSubmit", prompt="go")
        time.sleep(1)
        self.assertEqual([b["state"] for b in self.bodies()], ["ended"])
        self.hook("SessionStart", source="resume")
        self.settled()
        self.assertEqual([b["state"] for b in self.bodies()], ["ended", "idle"], "a resume did not report")

    def test_a_late_event_that_passed_the_end_check_still_cannot_outvote_the_end(self):
        # It read the state before SessionEnd wrote `ended`, then took the
        # lock after it: under the lock it must see the end and back off.
        self.hook("SessionEnd", reason="exit")
        self.settled()
        late = ("import sys; sys.path.insert(0, %r); from unio_sessions import hook; "
                "hook.request_presence(%r, {'state': 'idle'}, reopen=%s)")
        def send(reopen):
            subprocess.run([sys.executable, "-X", "utf8", "-I", "-c",
                            late % (str(SCRIPTS), self.session, reopen)], env=self.env(), check=True, timeout=20)
            self.settled()
            time.sleep(0.5)
            return [b["state"] for b in self.bodies()]

        self.assertEqual(send(False), ["ended"], "a late idle outvoted the end")
        self.assertEqual(send(True), ["ended", "idle"], "a new start could not reopen the session")

    def test_a_task_outside_the_agents_teams_still_shows_the_session(self):
        self.bind(task_id="99999999-0000-4000-8000-000000000000")
        self.hook("UserPromptSubmit", prompt="go")
        self.settled()
        self.assertEqual([(b["state"], b.get("taskId")) for b in self.bodies()],
                         [("working", "99999999-0000-4000-8000-000000000000"), ("working", None)])
        self.assertEqual(self.api.presences[self.presence_id]["state"], "working")

    def test_a_hanging_api_never_holds_the_hook(self):
        self.api.hang.set()
        elapsed, _ = self.hook("SessionStart", source="startup")
        self.assertLess(elapsed, HOOK_LIMIT)
        p = self.settled(limit=10)
        self.assertEqual(p["lastStatus"], 0)
        self.assert_no_children_left()
        self.api.hang.clear()

    def test_an_inbox_run_reports_and_binds_nothing(self):
        env = self.env(UNIO_INBOX_RUN="1")
        _, out = self.hook("SessionStart", env=env, source="startup")
        self.hook("UserPromptSubmit", env=env, prompt="https://unio.so/app/tasks/DEV-101")
        self.hook("PostToolUse", env=env, tool_name="mcp__plugin_unio_unio__get_task", tool_input={"task": "DEV-101"},
                  tool_response=self.get_task_result())
        time.sleep(1)
        self.assertEqual((out, self.api.requests), ("", []))
        self.assertFalse(self.sessions.exists() and any(self.sessions.iterdir()), "an inbox run left session state")

    def test_a_missing_or_malformed_token_reports_nothing(self):
        for content in (None, "not a token\n", fake_api.TOKEN[:-1]):
            with self.subTest(content):
                self.session = str(uuid.uuid4())
                if content is None:
                    self.token_file.unlink()
                else:
                    self.token_file.write_text(content)
                self.hook("SessionStart", source="startup")
                time.sleep(0.5)
                self.assertEqual(self.api.requests, [])
                self.assertEqual(self.presence(), {})

    def test_an_api_outside_the_allowed_shapes_gets_nothing(self):
        for value in ("", "http://example.com/v1", self.api.base[:-3], "ftp://127.0.0.1:1/v1"):
            with self.subTest(value):
                self.hook("SessionStart", env=self.env(UNIO_SESSION_API=value), source="startup")
        time.sleep(0.5)
        self.assertEqual(self.api.requests, [])

    def test_a_session_id_that_is_not_a_plain_name_is_ignored(self):
        self.session = "../escape"
        self.hook("SessionStart", source="startup")
        time.sleep(0.5)
        self.assertEqual(self.api.requests, [])
        self.assertFalse((self.tmp / "state" / "unio" / "escape.json").exists())

    def test_old_state_files_go_after_30_days(self):
        self.sessions.mkdir(parents=True)
        old = self.sessions / "old-session.json"
        old.write_text("{}")
        os.utime(old, (time.time() - 31 * 86400,) * 2)
        self.hook("Stop")
        self.assertFalse(old.exists())


class SessionWatcher(HookCase):
    """Under a Claude Code process, any event starts the session's watcher, which
    then reports in the hook's place until the session or the process ends.
    On Linux and macOS the hook runs as the plugin registers it, `sh
    session-status.sh` under a shell, with the token and API the launcher
    reads from ~/.config/unio."""

    TIMES = {"tick": 0.2, "list": 0.5, "endAfter": 1, "ghostEvery": 0.5, "saveEvery": 0.2, "stepSpacing": 1,
             "presenceRepeat": 2, "putSpacing": 0.5}

    def setUp(self):
        super().setUp()
        source = (Path(__file__).parent / "fake_claude.py").read_text()
        self.claude = self.tmp / "bin" / ("claude.py" if WINDOWS else "claude")
        self.claude.parent.mkdir()
        self.claude.write_text(f"#!{sys.executable}\n" + source)
        self.claude.chmod(0o755)
        self.transcript = self.tmp / ".claude" / "projects" / "-work" / f"{self.session}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        conf = self.tmp / ".config" / "unio"
        conf.mkdir(parents=True)
        (conf / "token").write_text(fake_api.TOKEN)
        (conf / "api-url").write_text(self.api.base[:-len("/v1")] + "\n")
        self.addCleanup(self.stop_processes)

    def env(self, **extra):
        times = {**self.TIMES, **json.loads(extra.pop("UNIO_WATCH_TIMES", "{}"))}
        env = super().env(UNIO_WATCH_TIMES=json.dumps(times), FAKE_CLAUDE_TRANSCRIPT=str(self.transcript),
                          CLAUDE_PROJECT_DIR=str(self.tmp), **extra)
        if WINDOWS:
            env["FAKE_CLAUDE_HOOK"] = json.dumps([sys.executable, "-I", str(ENTRY), "hook"])
        else:
            # The launcher resolves both from ~/.config/unio, as in a real session.
            del env["UNIO_SESSION_TOKEN_FILE"], env["UNIO_SESSION_API"]
            env["FAKE_CLAUDE_HOOK"] = json.dumps(["sh", "-c", f'sh "{LAUNCHER}"'])
        return env

    def write(self, *entries):
        with self.transcript.open("a", encoding="utf-8", newline="\n") as f:
            for e in entries:
                f.write(json.dumps({"sessionId": self.session, **e}) + "\n")

    @staticmethod
    def at(seconds_ago=0):
        return fake_api.iso(datetime.now(timezone.utc) - timedelta(seconds=seconds_ago))

    def watch_state(self):
        try:
            return json.loads((self.sessions / f"{self.session}.watch.json").read_text())
        except (OSError, ValueError):
            return {}

    def watchers(self):
        """The session's watchers: by their arguments, or on Windows by the
        lock one holds and the pid it saved."""
        if not WINDOWS:
            return processes("watch", "--session", self.session)
        from unio_sessions import system
        lock = self.sessions / f"{self.session}.watch.lock"
        if not lock.exists():
            return []
        with open(lock, "a") as f:
            if system.try_lock(f):
                system.unlock(f)
                return []
        pid = self.watch_state().get("watcherPid")
        return [pid] if pid else [-1]

    def stop_processes(self):
        """Before the state folder goes: a watcher still running would recreate it."""
        for pid in self.watchers() + processes("send-presence", self.session):
            if pid > 0:
                kill(pid)
        deadline = time.monotonic() + 5
        while self.watchers() and time.monotonic() < deadline:
            time.sleep(0.05)

    def start_claude(self, *events, **env):
        """The fake Claude Code, once its hooks for `events` have returned; their durations."""
        # Through its #! line elsewhere, so Linux names the process claude.
        argv = [sys.executable, str(self.claude)] if WINDOWS else [str(self.claude)]
        proc = subprocess.Popen([*argv, self.session, *events], cwd=str(self.tmp),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=self.env(**env))
        self.addCleanup(self.stop_claude, proc)
        return proc, [float(proc.stdout.readline()) for _ in events]

    @staticmethod
    def event(proc, event, **fields):
        proc.stdin.write(json.dumps({"hook_event_name": event, **fields}) + "\n")
        proc.stdin.flush()
        return float(proc.stdout.readline())

    @staticmethod
    def stop_claude(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout):
            if stream:
                stream.close()

    def test_the_start_runs_one_watcher_which_reports_what_the_session_does(self):
        self.write({"type": "user", "timestamp": self.at(30), "origin": {"kind": "human"},
                    "message": {"role": "user", "content": "run the tests"}},
                   {"type": "assistant", "timestamp": self.at(20), "message": {
                       "id": "msg_1", "model": "claude-opus-5-5", "stop_reason": "tool_use",
                       "usage": {"input_tokens": 3, "output_tokens": 40, "cache_read_input_tokens": 900,
                                 "cache_creation_input_tokens": 70},
                       "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                    "input": {"command": "cd backend && TZ=UTC node ace test functional"}}]}})
        proc, [elapsed] = self.start_claude("SessionStart")
        self.assertLess(elapsed, 2 * HOOK_LIMIT, "the hook waited on its watcher")
        wait_for(self, lambda: self.watchers(), 5, "no watcher started")
        wait_for(self, lambda: any(b.get("step") == "running_tests" for b in self.bodies()), 20,
                 lambda: f"the step never reached the page: {self.bodies()}")
        body = next(b for b in self.bodies() if b.get("step") == "running_tests")
        self.assertEqual(body, {"kind": "session", "host": HOST, "client": "Claude Code 2.1.287",
                                "state": "working", "step": "running_tests"})
        totals = self.api.session_totals(self.presence_id)
        # The 10 s from the prompt to the call, then the tests, running for 20 s
        # and counting: a tool still running is active time up to now.
        self.assertGreaterEqual(totals.pop("activeMs"), 30_000)
        self.assertEqual(totals, {"inputTokens": 3, "outputTokens": 40, "cacheReadTokens": 900,
                                  "cacheWriteTokens": 70})
        row = self.api.sessions[self.presence_id]
        self.assertNotIn(str(self.tmp), json.dumps([r["body"] for r in self.api.requests], default=str),
                         "a folder reached the server")
        self.assertEqual(row["host"], HOST)
        # A resume or a compaction in the same process keeps the one watcher.
        self.event(proc, "SessionStart", source="compact")
        self.event(proc, "Stop")
        time.sleep(1)
        self.assertEqual(len(self.watchers()), 1)

    def test_while_the_watcher_runs_the_hook_reports_nothing_but_the_end(self):
        self.bind()
        self.write({"type": "system", "subtype": "turn_duration", "timestamp": self.at(60), "durationMs": 1})
        proc, _ = self.start_claude("SessionStart")
        wait_for(self, lambda: self.bodies(), 10, "the watcher reported nothing")
        # The hook alone would say working on the bound task; the transcript shows no turn.
        self.event(proc, "UserPromptSubmit", prompt="go")
        time.sleep(1.5)
        self.assertEqual({b["state"] for b in self.bodies()}, {"idle"})
        self.assertEqual({b.get("taskId") for b in self.bodies()}, {self.task})
        self.event(proc, "SessionEnd", reason="prompt_input_exit")
        wait_for(self, lambda: not self.watchers(), 10, "the watcher outlived its session")
        # The hook's own sender delivers the end, maybe after the watcher has gone.
        wait_for(self, lambda: self.bodies()[-1]["state"] == "ended", 10, "the end never reached the page")
        self.assertEqual(self.watch_state().get("endedBy"), "session_end")
        self.assertIsNotNone(self.api.sessions.get(self.presence_id, {}).get("endedAt"), "the row was never ended")

    def test_a_dead_claude_code_ends_the_session(self):
        self.write({"type": "user", "timestamp": self.at(5), "origin": {"kind": "human"},
                    "message": {"role": "user", "content": "go"}})
        proc, _ = self.start_claude("SessionStart")
        wait_for(self, lambda: self.watchers() and self.api.sessions.get(self.presence_id), 10, "no session row")
        proc.kill()
        proc.wait()
        wait_for(self, lambda: not self.watchers(), 10, "the watcher outlived Claude Code")
        wait_for(self, lambda: self.bodies()[-1]["state"] == "ended", 5, "a dead Claude Code left the session working")
        self.assertIsNotNone(self.api.sessions[self.presence_id]["endedAt"])
        self.assertEqual(self.watch_state().get("endedBy"), "process_gone")

    def test_a_watcher_that_died_is_started_again_by_the_next_event(self):
        proc, _ = self.start_claude("SessionStart")
        wait_for(self, lambda: self.watchers() and self.watch_state().get("watcherPid"), 5, "no watcher")
        first = self.watch_state()["watcherPid"]
        kill(first)
        wait_for(self, lambda: not self.watchers(), 5, "the watcher ignored SIGTERM")
        self.event(proc, "Stop")
        wait_for(self, lambda: self.watchers() and self.watch_state().get("watcherPid") != first, 5,
                 "the next event started no watcher")

    @unittest.skipIf(WINDOWS, "Windows reads no other process's environment; test_system covers its rule")
    def test_inbox_runs_and_child_sessions_start_no_watcher(self):
        for env in ({"UNIO_INBOX_RUN": "1"}, {"CLAUDE_CODE_CHILD_SESSION": "1"}):
            proc, _ = self.start_claude("SessionStart", **env)
            time.sleep(1)
            self.assertEqual(self.watchers(), [], env)
            self.stop_claude(proc)
        self.assertEqual(self.api.requests, [])

    def test_a_hook_run_by_another_program_reports_without_a_watcher(self):
        # Claude Code is further up, but the hook's parent is neither it nor its shell:
        # the walk must not climb past that program to tie a watcher to Claude Code.
        self.start_claude("SessionStart", FAKE_CLAUDE_VIA_PROGRAM="1")
        self.assertEqual(self.settled()["want"], {"state": "idle"})
        time.sleep(1)
        self.assertEqual(self.watchers(), [])

    def test_a_top_level_session_reports_although_its_hooks_carry_the_child_marker(self):
        # The fake gives its hook CLAUDE_CODE_CHILD_SESSION=1, as Claude Code does for every hook.
        self.start_claude("SessionStart")
        wait_for(self, lambda: self.watchers(), 5, "a top-level session started no watcher")
        wait_for(self, lambda: self.bodies(), 10, "the watcher reported nothing")

    def test_a_hook_run_by_hand_inside_another_session_starts_no_watcher(self):
        self.start_claude("SessionStart", FAKE_CLAUDE_OWN_SESSION=str(uuid.uuid4()))
        self.settled()
        time.sleep(1)
        self.assertEqual(self.watchers(), [])


if __name__ == "__main__":
    unittest.main()
