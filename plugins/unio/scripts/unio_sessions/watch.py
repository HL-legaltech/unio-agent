#!/usr/bin/env python3
"""Watch one Claude Code session's transcripts and report what it does to Unio.

    sessions.py watch --session <id> --pid <claude pid> --pid-start <start time>
                      [--transcript <path>] [--source startup|resume|clear|compact|fork]
                      [--model <id>] [--event-at <time of the hook event that started it>]
                      [--reexec (added when it restarts itself on new code)]

hook.py starts it from any hook event while <session>.watch.lock is free; one
runs per session id, for as long as its Claude Code process lives.
It reads the bytes each transcript grew by (the main one and every subagent
and workflow agent file under <session>/subagents) and sends two things:

  presence     PUT /agents/presence/<id> through the hook's own functions, so
               presence.json keeps the protocol the hook and the heartbeats
               it used to start share: state, task and a step code naming
               what the session does (running the tests, editing the code,
               waiting for a person...); a problem carries its reason
  the session  PUT /agents/sessions/<id>: when it started and was last
               active, its task, model, context, running subagents, and per
               UTC hour its tokens and active time; only what changed

Both ids are uuid5(NAMESPACE_URL, "unio-session:<session id>"). Tokens count
one API call per message.id, the last entry winning (subagent entries repeat
a call's usage as its output grows), in the hour of the call's first entry,
never <synthetic> ones. A continued session's file holds copies of the
conversation it continues under their old times, after its own first entry
or ahead of everything when the file appears after the watcher started:
both are skipped. A session found already running (no state, and not a
fresh start or /clear) is adopted at the end of its files and says
`countedSince`: reading gigabytes of history cold would take hours here.

State, with each file's cursor, lives in <session>.watch.json beside the
hook's files. On Linux and macOS it re-execs itself when one of its package's
files changes and compiles (a plugin loaded from a folder being edited);
Windows has no exec that keeps the process and its lock, and an installed
plugin's files never change in place. Never prints; every failure is recorded
in the state and retried.
"""
import argparse
import json
import os
import re
import signal
import socket
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import hook as session_hook
from . import system

HERE = Path(os.path.abspath(__file__)).parent
# Every file whose change re-execs a running watcher.
CODE = sorted(HERE.glob("*.py")) + [HERE.parent / "sessions.py"]
ENTRY = HERE.parent / "sessions.py"

# Tests shorten them through UNIO_WATCH_TIMES.
TIMES = {
    "tick": 2.0,
    "list": 10.0,
    "cold": 600.0,            # a file that has not grown this long leaves the per-tick read
    "endAfter": 120.0,        # the process is gone and nothing grew this long: ended
    "ghostEvery": 10.0,
    "saveEvery": 30.0,        # cursors and counts are saved together: a stale save only rereads
    "presenceRepeat": 40.0,   # under the old heartbeat's 45 s, so a running one never fires
    "stepSpacing": 11.0,      # the server stores a repeat at most every 10 s
    "quietIdle": 900.0,
    "quietWaiting": 1800.0,
    "putSpacing": 15.0,
    "keepalive": 600.0,       # the web app reads a session `open` while reported within 15 min
    "routeBackoff": 3600.0,
    "limitBackoff": 300.0,
    "retryMax": 300.0,
}
try:
    TIMES.update({k: float(v) for k, v in json.loads(os.environ.get("UNIO_WATCH_TIMES") or "{}").items()})
except (ValueError, TypeError, AttributeError):
    pass

GAP = 300            # active time: gaps between entries up to 5 min count
THINKING_AFTER = 15  # a generation gap reads "thinking" once it held this long...
HOLD_STEP = 20       # ...and the last tool's step is held through shorter ones
BG_BASH_LIVE = 1800  # a background command keeps the session working this long at most
# An agent that wrote nothing for this long is not running: its process was
# killed or its conversation moved on (a continued session's workflow keeps its
# journal's `started` lines and never gets a result). Agents' longest silent
# tool runs measured 586-648 s (10-minute test suites).
AGENT_STALE = 1800
# ...unless it sits at a usage limit, which holds an agent silent for hours
# (147 min measured) while it still runs; a limit window is 5 h.
LIMIT_STALE = 6 * 3600
TAIL = 2 * 1024 * 1024
AGENT_TAIL = 256 * 1024
HEAD_BYTES = 1024 * 1024
CHUNK = 8 * 1024 * 1024
# Entries stamped this long before a file's first one are copies. In the heads
# of the 128 transcripts of the 20 days to 2026-10-06, a file's own entries ran
# at most 0.23 s early and a continued session's copies at least 228 s.
COPY_LAG = 60
# A transcript the watcher saw missing holds only entries stamped since, give or
# take this; older ones are copies too. Claude Code creates the file within
# 1.2 s of stamping its first prompt (40 recent files); the SessionStart hook's
# attachments it held until then are older, and skipping them only starts the
# session at that prompt.
SKIP_SLACK = 30
GHOST_STALE = 300    # a live watcher saves at least once a minute
HOUR_MS = 3_600_000
MAX_HOURS = 500
KEEP_DAYS = 90
STATE_VERSION = 1

WAIT_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
DELEGATING = {"Agent", "Task", "Workflow"}
NEW_STEPS = {"thinking", "searching_code", "editing_code", "running_command", "running_tests",
             "running_checks", "using_git", "researching", "delegating", "waiting_for_person"}
TOOL_STEPS = {
    "Read": "reading_code", "NotebookRead": "reading_code",
    "Grep": "searching_code", "Glob": "searching_code", "LS": "searching_code",
    "Edit": "editing_code", "MultiEdit": "editing_code", "Write": "editing_code", "NotebookEdit": "editing_code",
    "WebSearch": "researching", "WebFetch": "researching",
    "Agent": "delegating", "Task": "delegating", "Workflow": "delegating",
    "AskUserQuestion": "waiting_for_person", "ExitPlanMode": "waiting_for_person",
    "mcp__unio__get_task": "reading_task", "mcp__unio__list_comments": "reading_thread",
    "mcp__unio__search_tasks": "searching_tasks", "mcp__unio__create_comment": "writing_reply",
    "mcp__unio__update_comment": "writing_reply", "mcp__unio__update_task": "updating_task",
    "mcp__unio__add_reaction": "reacting", "mcp__unio__remove_reaction": "reacting",
    "mcp__unio__search_projects": "searching_projects", "mcp__unio__get_project": "reading_project",
    "mcp__unio__create_project": "creating_project", "mcp__unio__update_project": "updating_project",
    "mcp__unio__post_project_update": "updating_project",
}
TESTS = re.compile(r"(^|[\s/])(pytest|py\.test|vitest|jest|mocha|phpunit|rspec|playwright\s+test|japa)\b"
                   r"|\bunittest\b|\bace\s+test\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?test\b|\bgo\s+test\b"
                   r"|\bcargo\s+(test|nextest)\b|\bmake\s+(test|check)\b|\btest_\w+\.py\b")
CHECKS = re.compile(r"(^|[\s/])(tsc|eslint|biome|ruff|mypy|pyright|flake8|shellcheck|stylelint|oxlint)\b"
                    r"|\btypecheck\b|\blint\b|\bprettier\s+(--check|-c)\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?build\b"
                    r"|\bnext\s+build\b|\bcargo\s+(check|clippy|build)\b|\bgo\s+(vet|build)\b|\bpy_compile\b")
GIT = re.compile(r"^(sudo\s+)?(git|gh)\b")
LOCAL = ("<command-name>", "<command-message>", "<local-command-stdout>", "<local-command-stderr>",
         "<local-command-caveat>", "<bash-input>", "<bash-stdout>", "<bash-stderr>", "<user-memory-input>")
NOTICE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
NOTICE_ID = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
NOTICE_STATUS = re.compile(r"<status>([^<]+)</status>")
MODEL = re.compile(r"^claude-[a-z0-9.-]{1,48}(\[1m\])?$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LIMIT_TEXT = ("Usage limit reached", "hit your session limit", "hit your usage limit")


STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?")


def stamp(value):
    """Seconds since 1970 of an ISO 8601 time; Python 3.9's fromisoformat
    reads neither a Z nor a fraction of other than 3 or 6 digits."""
    if not isinstance(value, str):
        return None
    m = STAMP.fullmatch(value.strip())
    if not m:
        return None
    try:
        y, mo, d, h, mi, sec = (int(x) for x in m.groups()[:6])
        zone = m.group(8)
        if zone in (None, "Z"):
            tz = timezone.utc if zone else None
        else:
            sign = 1 if zone[0] == "+" else -1
            tz = timezone(sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])))
        frac = m.group(7) or ""
        t = datetime(y, mo, d, h, mi, sec, tzinfo=tz).timestamp()
        return t + (int(frac) / 10 ** len(frac) if frac else 0)
    except (ValueError, OverflowError):
        return None


def iso(t):
    d = datetime.fromtimestamp(t, timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def hour_of(t):
    return int(t // 3600) * 3600


def bash_step(command):
    """What a shell command does, judged on each of its segments: the tests
    outrank the checks, which outrank git."""
    found = "running_command"
    for part in re.split(r"&&|\|\||;|\||\n", command or ""):
        part = part.strip()
        while re.match(r"^(\w+=\S*\s+|cd\s+\S+\s*$|env\s+|timeout\s+\S+\s+|nice\s+(-n\s*\S+\s+)?|time\s+)", part):
            part = re.sub(r"^(\w+=\S*\s+|cd\s+\S+\s*$|env\s+|timeout\s+\S+\s+|nice\s+(-n\s*\S+\s+)?|time\s+)", "",
                          part, count=1)
        if TESTS.search(part):
            return "running_tests"
        if CHECKS.search(part):
            found = "running_checks"
        elif GIT.match(part) and found == "running_command":
            found = "using_git"
    return found


def tool_step(name, args):
    if isinstance(name, str) and name.startswith("mcp__plugin_unio_unio__"):
        name = "mcp__unio__" + name[len("mcp__plugin_unio_unio__"):]
    if name == "Bash":
        return bash_step((args or {}).get("command"))
    if name == "mcp__unio__create_task":
        return "filing_follow_up" if (args or {}).get("follow_up_of") else None
    return TOOL_STEPS.get(name)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def presence_id(session):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"unio-session:{session}"))


class Intervals:
    """Active time as merged intervals, kept per UTC hour, so a span read late
    from another file (after a long outage, say) still merges with what the
    hour already holds. Whole milliseconds, so the parts of a span add up to it."""

    def __init__(self, data=None):
        self.hours = {int(h): [list(x) for x in iv] for h, iv in ((data or {}).get("hours") or {}).items()}

    def dump(self):
        return {"hours": {str(h): iv for h, iv in self.hours.items()}}

    def add(self, a, b):
        a, b = round(a * 1000), round(b * 1000)
        h = a // HOUR_MS * HOUR_MS
        while h < b:
            lo, hi = max(a, h), min(b, h + HOUR_MS)
            if hi > lo:
                self.merge(h // 1000, lo, hi)
            h += HOUR_MS

    def merge(self, hour, a, b):
        out = []
        for x in self.hours.get(hour, []):
            if x[1] < a or x[0] > b:
                out.append(x)
            else:
                a, b = min(a, x[0]), max(b, x[1])
        out.append([a, b])
        out.sort()
        self.hours[hour] = out

    def per_hour(self):
        return {h: sum(b - a for a, b in iv) for h, iv in self.hours.items()}


class Model:
    """Everything the transcripts say about the session, kept as plain data so
    it survives a restart or a re-exec in <session>.watch.json."""

    def __init__(self, main_path, state=None, now=None):
        self.set_main(main_path)
        s = state or {}
        self.s = s
        s.setdefault("v", STATE_VERSION)
        s.setdefault("files", {})
        s.setdefault("hours", {})
        s.setdefault("turn", {"open": False, "at": None})
        s.setdefault("bg", {})
        s.setdefault("stops", {})
        s.setdefault("clearAt", 0)
        s.setdefault("lastGrowthAt", now or time.time())
        self.active = Intervals(s.get("active"))
        self.listed_at = 0
        self.dirty = False

    def set_main(self, main_path):
        self.main_path = Path(main_path) if main_path else None
        self.subagents = self.main_path.with_suffix("") / "subagents" if self.main_path else None

    def file(self, key):
        return self.s["files"].get(key)

    def path_of(self, key):
        return self.main_path if key == "main" else self.subagents.parent / key

    def new_file(self, key, kind, **extra):
        f = {"kind": kind, "inode": None, "offset": 0, "size": 0, "firstTs": None, "lastMessageId": None,
             "lastMessageUsage": [0, 0, 0, 0], "lastMessageHour": None, "point": None, "limitBlock": False,
             "pending": {}, "lastStep": None, "lastStepAt": None, "limitAt": None, "authAt": None,
             "grewAt": 0, **extra}
        self.s["files"][key] = f
        return f

    def adopt(self, fresh, now, model_hint=None):
        """A session seen for the first time: read from the start, or join it at
        the end of its files with what their tails say."""
        if model_hint and MODEL.match(model_hint):
            self.s["modelId"] = model_hint
        main = self.new_file("main", "main")
        self.s["adopted"] = "start" if fresh else "end"
        if not fresh:
            self.s["countedSince"] = now
        if self.main_path and self.main_path.exists():
            if fresh:
                self.read(main, "main", now)
            else:
                self.join_at_end(main, "main", TAIL, now)
        else:
            self.missing(main, now)
        self.list_subagents(now, initial=not fresh)
        self.dirty = True

    @staticmethod
    def missing(f, now):
        """The main transcript is not written yet. A continued session's file
        opens with the conversation it continues, under its old times."""
        if f["inode"] is None:
            f["skipBefore"] = max(f.get("skipBefore") or 0, now - SKIP_SLACK)

    def join_at_end(self, f, key, tail, now, head=True):
        """Starts reading at the end. The file's first timestamp is read from its
        head now, or, for a file nobody writes, once it grows again."""
        path = self.path_of(key)
        try:
            st = path.stat()
        except OSError:
            return
        if head:
            f["firstTs"], model = self.head(path)
            # A tail rarely reaches back to the attachment that names a 1M context.
            if key == "main" and model and not self.s.get("modelId"):
                self.s["modelId"] = model
        else:
            f["firstTs"], f["headPending"] = None, True
        f["inode"], f["size"] = st.st_ino, st.st_size
        start = max(0, st.st_size - tail)
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(st.st_size - start)
        cut = data.rfind(b"\n") + 1
        lines = data[:cut].split(b"\n")
        if start:
            lines = lines[1:]  # the first one is cut
        for line in lines:
            self.line(f, line, counting=False)
        f["offset"] = start + cut
        f["grewAt"] = now

    @staticmethod
    def head(path):
        """The file's first timestamp, and the model its `model` attachment
        names, which comes with the first prompt (before the first call in 92
        of 94 recent transcripts that have one)."""
        first = model = None
        seen = 0
        try:
            with open(path, "rb") as fh:
                for n, line in enumerate(fh):
                    seen += len(line)
                    if n > 2000 or first is not None and seen > HEAD_BYTES:
                        break
                    if b'"timestamp"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(e, dict):
                        continue
                    if first is None:
                        first = stamp(e.get("timestamp"))
                    a = e.get("attachment")
                    if isinstance(a, dict) and a.get("type") == "model":
                        found = (a.get("identity") or {}).get("modelId")
                        model = found if isinstance(found, str) else model
                    if first is not None and (model or e.get("type") == "assistant"):
                        break
        except OSError:
            pass
        return first, model

    @staticmethod
    def first_stamp(path):
        return Model.head(path)[0]

    def read(self, f, key, now):
        """Consumes the complete lines a file grew by; True when it grew."""
        path = self.path_of(key)
        try:
            st = path.stat()
        except OSError:
            return False
        if f["inode"] is None:
            f["inode"] = st.st_ino
        elif f["inode"] != st.st_ino or st.st_size < f["offset"]:
            # Rewritten or replaced: join it again at its end rather than count it twice.
            f["lastMessageId"] = None
            self.join_at_end(f, key, 0, now)
            return True
        f["size"] = st.st_size
        if st.st_size <= f["offset"]:
            return False
        if f.pop("headPending", False):
            f["firstTs"] = self.first_stamp(path)
        grew = False
        with open(path, "rb") as fh:
            fh.seek(f["offset"])
            while f["offset"] < st.st_size:
                data = fh.read(min(CHUNK, st.st_size - f["offset"]))
                cut = data.rfind(b"\n") + 1
                while not cut and f["offset"] + len(data) < st.st_size and len(data) < 32 * CHUNK:
                    more = fh.read(CHUNK)
                    if not more:
                        break
                    data += more
                    cut = data.rfind(b"\n") + 1
                if not cut:
                    break
                for line in data[:cut].split(b"\n"):
                    self.line(f, line, counting=True)
                f["offset"] += cut
                fh.seek(f["offset"])
                grew = True
        if grew:
            f["grewAt"] = now
            self.s["lastGrowthAt"] = now
            self.dirty = True
        return grew

    def line(self, f, line, counting):
        if not line.strip():
            return
        try:
            e = json.loads(line)
        except ValueError:
            return
        if isinstance(e, dict):
            self.entry(f, e, counting)

    def list_subagents(self, now, initial=False):
        """Finds new agent files and journals; joins the ones present when a
        session is adopted at their end, and reads new ones from the start."""
        self.listed_at = now
        if not self.subagents or not self.subagents.is_dir():
            return
        base = self.subagents.parent
        for dirpath, _, names in os.walk(self.subagents):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                key = os.path.relpath(os.path.join(dirpath, name), base)
                f = self.file(key)
                if f is None:
                    if name == "journal.jsonl":
                        f = self.new_file(key, "journal", runId=os.path.basename(dirpath), started=[], results=[])
                        self.read(f, key, now)
                        continue
                    if not name.startswith("agent-"):
                        continue
                    agent = name[len("agent-"):-len(".jsonl")]
                    in_workflow = os.path.basename(os.path.dirname(dirpath)) == "workflows"
                    run = os.path.basename(dirpath) if in_workflow else None
                    f = self.new_file(key, "agent", agentId=agent, runId=run, toolUseId=None)
                    self.meta_of(f, dirpath)
                    if initial:
                        try:
                            recent = now - os.stat(os.path.join(dirpath, name)).st_mtime < TIMES["cold"]
                        except OSError:
                            recent = False
                        self.join_at_end(f, key, AGENT_TAIL if recent else 0, now, head=recent)
                        if not recent:
                            f["grewAt"] = 0
                        continue
                    self.read(f, key, now)
                    continue
                if f["kind"] == "agent" and f["toolUseId"] is None and f["runId"] is None:
                    self.meta_of(f, dirpath)
                try:
                    size = os.stat(os.path.join(dirpath, name)).st_size
                except OSError:
                    continue
                if size != f["size"] or size > f["offset"]:
                    self.read(f, key, now)

    @staticmethod
    def meta_of(f, dirpath):
        """The tool call that started an agent, from the meta file beside it
        (which may be written a moment after the agent's own file)."""
        try:
            meta = json.loads((Path(dirpath) / f"agent-{f['agentId']}.meta.json").read_text())
            f["toolUseId"] = meta.get("toolUseId") if isinstance(meta, dict) else None
        except (OSError, ValueError):
            pass

    def poll(self, now):
        """Reads what grew since the last tick; the folder is listed at most
        every TIMES["list"]."""
        main = self.file("main")
        if main is None:
            return False
        grew = False
        if self.main_path and self.main_path.exists():
            grew = self.read(main, "main", now)
        elif self.main_path:
            self.missing(main, now)
        hot = self.running_files(now) | {f"subagents/workflows/{b['runId']}/journal.jsonl"
                                      for b in self.s["bg"].values() if b["kind"] == "workflow" and b.get("runId")}
        for key, f in list(self.s["files"].items()):
            if key == "main":
                continue
            if key in hot or now - f.get("grewAt", 0) < TIMES["cold"]:
                grew = self.read(f, key, now) or grew
        if now - self.listed_at >= TIMES["list"]:
            self.list_subagents(now)
        return grew

    def entry(self, f, e, counting):
        t = stamp(e.get("timestamp"))
        if t is not None:
            if t < (f.get("skipBefore") or 0) or f["firstTs"] is not None and t < f["firstTs"] - COPY_LAG:
                self.copied(f, e)
                return
            if f["firstTs"] is None:
                f["firstTs"] = t
        kind = e.get("type")
        if f["kind"] == "journal":
            self.journal(f, e)
        elif kind == "assistant":
            self.assistant(f, e, t, counting)
        elif kind == "user":
            self.user(f, e, t, counting)
        elif kind == "system":
            self.system(f, e, t)
        elif f["kind"] != "main":
            return
        elif kind == "queue-operation":
            self.notices(e.get("content"))
        elif kind == "attachment":
            a = e.get("attachment") or {}
            if a.get("type") == "model":
                model = (a.get("identity") or {}).get("modelId")
                if isinstance(model, str):
                    self.s["modelId"] = model
            elif a.get("type") == "queued_command":
                self.notices(a.get("prompt"))
        elif kind == "ai-title" and isinstance(e.get("aiTitle"), str):
            self.s["title"] = e["aiTitle"]
        elif kind == "continued-in":
            # The conversation goes on under another session id: nothing this one
            # had in flight will report back here.
            self.close_turn()
            self.s["bg"] = {}
            self.s["continuedAt"] = t if t is not None else time.time()

    def copied(self, f, e):
        """An entry copied from the conversation this one continues: never
        counted, though its model attachment names the model while nothing
        else has (one of three continued sessions measured writes none of its own)."""
        a = e.get("attachment") if f["kind"] == "main" else None
        if isinstance(a, dict) and a.get("type") == "model" and not self.s.get("modelId"):
            model = (a.get("identity") or {}).get("modelId")
            if isinstance(model, str):
                self.s["modelId"] = model

    def journal(self, f, e):
        agent = e.get("agentId")
        if e.get("type") == "started" and agent and agent not in f["started"]:
            f["started"].append(agent)
        elif e.get("type") in ("result", "failed") and agent and agent not in f["results"]:
            f["results"].append(agent)

    def point(self, f, t, counting):
        """An assistant or user entry: the gap since the file's last one counts as
        active time unless the file sat waiting on a person or a usage limit."""
        if t is None:
            return
        last = f["point"]
        waiting = any(p[1] in WAIT_TOOLS for p in f["pending"].values())
        if counting and last is not None and 0 < t - last <= GAP and not waiting and not f["limitBlock"]:
            self.add_active(last, t)
        if last is None or t > last:
            f["point"] = t
        if t > (self.s.get("lastActivityAt") or 0):
            self.s["lastActivityAt"] = t

    def add_active(self, a, b):
        since = self.s.get("countedSince")
        if since:
            a = max(a, since)
        if b > a:
            self.active.add(a, b)

    def assistant(self, f, e, t, counting):
        m = e.get("message") or {}
        main = f["kind"] == "main"
        self.point(f, t, counting)
        content = m.get("content") if isinstance(m.get("content"), list) else []
        if m.get("model") == "<synthetic>":
            text = text_of(content)
            if e.get("error") == "rate_limit" or any(x in text for x in LIMIT_TEXT):
                f["limitAt"], f["limitBlock"] = t, True
            elif e.get("error") == "authentication_failed" or "Not logged in" in text:
                f["authAt"], f["limitBlock"] = t, True
            if main and m.get("stop_reason") == "stop_sequence":
                self.close_turn()
            return
        f["limitBlock"] = False
        if t is not None and t > self.s["clearAt"]:
            self.s["clearAt"] = t
        u = m.get("usage")
        if isinstance(u, dict):
            mid = m.get("id") or f"noid:{e.get('uuid')}"
            vec = [int(u.get(k) or 0) for k in
                   ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
            if mid != f["lastMessageId"]:
                f["lastMessageId"], f["lastMessageUsage"] = mid, [0, 0, 0, 0]
                f["lastMessageHour"] = hour_of(t) if t is not None else None
            if counting and f["lastMessageHour"] is not None:
                bucket = self.s["hours"].setdefault(str(f["lastMessageHour"]), [0, 0, 0, 0])
                for i in range(4):
                    bucket[i] += vec[i] - f["lastMessageUsage"][i]
            f["lastMessageUsage"] = vec
            if main:
                context = vec[0] + vec[2] + vec[3]
                if context:
                    self.s["context"] = context
                    if context > 200_000:
                        self.s["over200k"] = True
        if main:
            if isinstance(m.get("model"), str):
                self.s["msgModel"] = m["model"]
            if m.get("stop_reason") == "end_turn":
                self.close_turn()
            elif not self.s["turn"]["open"] and t is not None:
                self.s["turn"] = {"open": True, "at": t}
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                f["pending"][b["id"]] = [t or 0, b.get("name"), tool_step(b.get("name"), b.get("input"))]
                if main and b.get("name") == "TaskStop":
                    task = (b.get("input") or {}).get("task_id") or (b.get("input") or {}).get("shell_id")
                    if task:
                        self.s["stops"][b["id"]] = task

    def user(self, f, e, t, counting):
        m = e.get("message") or {}
        content = m.get("content")
        main = f["kind"] == "main"
        self.point(f, t, counting)
        f["limitBlock"] = False
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] \
            if isinstance(content, list) else []
        if results:
            for b in results:
                started = f["pending"].pop(b.get("tool_use_id"), None)
                if not started:
                    continue
                if counting and started[1] not in WAIT_TOOLS and t is not None:
                    self.add_active(started[0], t)
                if t is not None:
                    f["lastStep"], f["lastStepAt"] = started[2], t
                if main:
                    self.launched(b, started, e.get("toolUseResult") if len(results) == 1 else None, t)
            if main and text_of(content).startswith("[Request interrupted"):
                self.close_turn()
            return
        if not main:
            return
        text = text_of(content)
        origin = (e.get("origin") or {}).get("kind")
        if text.startswith("[Request interrupted"):
            self.close_turn()
        elif self.opens_turn(e, text, origin) and t is not None:
            self.s["turn"] = {"open": True, "at": t}
        if origin == "auto-continuation" and t is not None:
            self.s["clearAt"] = max(self.s["clearAt"], t)
        self.notices(text)

    @staticmethod
    def opens_turn(e, text, origin):
        if e.get("isCompactSummary") or e.get("isVisibleInTranscriptOnly"):
            return False
        if e.get("isMeta"):
            return origin in ("peer", "task-notification", "auto-continuation")
        return not text.lstrip().startswith(LOCAL)

    def launched(self, block, started, result, t):
        """A tool result that started work in the background."""
        name = started[1]
        if block.get("is_error") or not isinstance(result, dict):
            return
        tid = block.get("tool_use_id")
        if name == "Bash" and result.get("backgroundTaskId"):
            self.s["bg"][tid] = {"kind": "bash", "taskId": result["backgroundTaskId"], "at": t, "step": started[2]}
        elif name in ("Agent", "Task") and result.get("isAsync") and result.get("agentId"):
            self.s["bg"][tid] = {"kind": "agent", "taskId": result["agentId"], "agentId": result["agentId"], "at": t}
        elif name == "Workflow" and result.get("taskId"):
            self.s["bg"][tid] = {"kind": "workflow", "taskId": result["taskId"], "runId": result.get("runId"), "at": t}
        elif name == "TaskStop":
            task = self.s["stops"].pop(tid, None)
            for key, task_info in list(self.s["bg"].items()):
                if task and task_info.get("taskId") == task:
                    del self.s["bg"][key]

    def notices(self, text):
        if not isinstance(text, str) or "<task-notification>" not in text:
            return
        for body in NOTICE.findall(text):
            ids, status = NOTICE_ID.findall(body), NOTICE_STATUS.search(body)
            if status and status.group(1).strip() not in ("running", "pending"):
                for tid in ids:
                    self.s["bg"].pop(tid.strip(), None)

    def system(self, f, e, t):
        sub = e.get("subtype")
        content = e.get("content") if isinstance(e.get("content"), str) else ""
        if sub == "informational" and t is not None:
            if "Usage limit reset" in content:
                self.s["clearAt"] = max(self.s["clearAt"], t)
            elif any(x in content for x in LIMIT_TEXT):
                f["limitAt"], f["limitBlock"] = t, True
        if f["kind"] == "main" and sub in ("turn_duration", "stop_hook_summary"):
            self.close_turn()

    def close_turn(self):
        self.s["turn"] = {"open": False, "at": None}
        main = self.file("main")
        if main:
            for tid in [k for k, p in main["pending"].items() if p[1] not in WAIT_TOOLS]:
                del main["pending"][tid]

    def new_process(self):
        """A new Claude Code process took the session over (a resume): nothing
        the old one had in flight runs any more."""
        self.s["turn"] = {"open": False, "at": None}
        self.s["bg"] = {}
        self.s["stops"] = {}
        for f in self.s["files"].values():
            if f["kind"] != "journal":
                f["pending"] = {}
        self.dirty = True

    def running_agents(self, now=None):
        """agent id -> its file's key (or None while not listed yet), leaving out
        agents silent for AGENT_STALE by their own entries' times."""
        now = time.time() if now is None else now
        found = {}
        files = self.s["files"]

        def fresh(key, since):
            f = files.get(key) if key else None
            last = max((f or {}).get("point") or 0, (f or {}).get("firstTs") or 0, since or 0)
            limited = bool((f or {}).get("limitBlock"))
            return now - last < (LIMIT_STALE if limited else AGENT_STALE)

        for info in self.s["bg"].values():
            if info["kind"] == "agent":
                key = f"subagents/agent-{info['agentId']}.jsonl"
                if fresh(key, info.get("at")):
                    found[info["agentId"]] = key
            elif info["kind"] == "workflow" and info.get("runId"):
                journal = files.get(f"subagents/workflows/{info['runId']}/journal.jsonl") or {}
                for agent in journal.get("started", []):
                    key = f"subagents/workflows/{info['runId']}/agent-{agent}.jsonl"
                    if agent not in journal.get("results", []) and fresh(key, info.get("at")):
                        found[agent] = key
        main = files.get("main") or {}
        for tid, p in main.get("pending", {}).items():
            if p[1] in ("Agent", "Task"):
                key = next((k for k, f in files.items() if f.get("toolUseId") == tid), None)
                if fresh(key, p[0]):
                    found[f"tool:{tid}" if key is None else files[key]["agentId"]] = key
        return found

    def live_background(self, now, agents):
        """The background tasks still running: a command for BG_BASH_LIVE at most, an
        agent while it runs, a workflow while one of its agents runs or its journal
        grew within AGENT_STALE, and either for AGENT_STALE after its launch while
        nothing of it has been written yet."""
        files = self.s["files"]
        live = []
        for b in self.s["bg"].values():
            fresh = now - (b.get("at") or now) < AGENT_STALE
            if b["kind"] == "bash":
                if now - (b.get("at") or now) < BG_BASH_LIVE:
                    live.append(b)
            elif b["kind"] == "agent":
                if b["agentId"] in agents or (fresh and f"subagents/agent-{b['agentId']}.jsonl" not in files):
                    live.append(b)
            elif b["kind"] == "workflow":
                # Between two phases every agent so far has its result and the next
                # has not started: the journal's own growth says the run goes on.
                journal = files.get(f"subagents/workflows/{b.get('runId')}/journal.jsonl")
                if journal is None:
                    if fresh:
                        live.append(b)
                elif any(a in agents for a in journal.get("started", [])) \
                        or now - (journal.get("grewAt") or 0) < AGENT_STALE:
                    live.append(b)
        return live

    def running_files(self, now=None):
        return {k for k in self.running_agents(now).values() if k}

    def problem(self, now=None):
        files = [self.file("main") or {}] + [self.file(k) or {} for k in self.running_files(now)]
        latest = None
        for f in files:
            for reason, at in (("usage_limit", f.get("limitAt")), ("signed_out", f.get("authAt"))):
                if at and at > self.s["clearAt"] and (latest is None or at > latest[1]):
                    latest = (reason, at)
        return latest[0] if latest else None

    def view(self, now):
        main = self.file("main") or {"pending": {}}
        turn = self.s["turn"]
        agents = self.running_agents(now)
        bg = self.live_background(now, agents)
        waiting = any(p[1] in WAIT_TOOLS for p in main["pending"].values())
        problem = self.problem(now)
        working = turn["open"] or bool(main["pending"]) or bool(bg) or bool(agents)
        state = "problem" if problem else "waiting" if waiting else "working" if working else "idle"
        step = None
        if state == "waiting":
            step = "waiting_for_person"
        elif state == "working":
            step = self.step(now, main, turn, agents, bg)
        return {"state": state, "step": step, "reason": problem, "activeAgents": len(agents)}

    def step(self, now, main, turn, agents, bg):
        mine = [p for p in main["pending"].values() if p[1] not in WAIT_TOOLS]
        theirs = []
        for key in agents.values():
            f = self.file(key) if key else None
            if f:
                theirs += f["pending"].values()
        newest_theirs = max(theirs, key=lambda p: p[0]) if theirs else None
        if mine:
            newest = max(mine, key=lambda p: p[0])
            if newest[1] in DELEGATING and newest_theirs:
                return newest_theirs[2]
            return newest[2]
        if newest_theirs:
            return newest_theirs[2]
        if turn["open"]:
            since = max(turn["at"] or 0, main.get("lastStepAt") or 0)
            held = main.get("lastStep") if (main.get("lastStepAt") or 0) >= (turn["at"] or 0) else None
            if held and now - since < HOLD_STEP:
                return held
            return "thinking" if now - since >= THINKING_AFTER else None
        if agents or any(b["kind"] != "bash" for b in bg):
            return "delegating"
        newest = max(bg, key=lambda b: b.get("at") or 0) if bg else None
        return newest.get("step") if newest else None

    def started_at(self):
        main = self.file("main") or {}
        return main.get("firstTs")

    def model(self):
        for m in (self.s.get("modelId"), self.s.get("msgModel")):
            if isinstance(m, str) and MODEL.match(m):
                return m
        return None

    def context_limit(self):
        model = self.s.get("modelId") or ""
        return 1_000_000 if "[1m]" in model or self.s.get("over200k") else 200_000

    def running_since(self, now):
        """When each tool still running began, in the main conversation and in the
        agents that run: a test suite in its tenth minute is active time already,
        not only once its result lands."""
        files = [self.file("main") or {}] + [self.file(k) or {} for k in self.running_files(now)]
        return [p[0] for f in files for p in f.get("pending", {}).values() if p[1] not in WAIT_TOOLS and p[0]]

    def activity_end(self, now):
        """The last moment the session was active: its last entry, or now while a
        tool runs."""
        last = self.s.get("lastActivityAt") or 0
        return max(last, now) if self.running_since(now) else last

    def buckets(self, now=None):
        """hour -> [input, output, cache read, cache write, active ms]."""
        active = self.active
        spans = self.running_since(now) if now is not None else []
        if spans:
            active = Intervals(self.active.dump())
            since = self.s.get("countedSince")
            for a in spans:
                a = max(a, since) if since else a
                if now > a:
                    active.add(a, now)
        found = {}
        for h, v in self.s["hours"].items():
            found[int(h)] = [max(0, x) for x in v] + [0]
        for h, ms in active.per_hour().items():
            found.setdefault(h, [0, 0, 0, 0, 0])[4] = ms
        return found

    def dump(self):
        self.s["active"] = self.active.dump()
        return self.s


class Reporter:
    """When to send presence and the session row, and what. The senders are
    injected, so the cadence can be tested with a clock that is not real."""

    def __init__(self, model, session, send_presence, quiet_presence, put_session, host, state=None):
        self.m, self.sid = model, session
        self.send_presence, self.quiet_presence, self.put_session = send_presence, quiet_presence, put_session
        self.host = host
        r = state if state is not None else {}
        self.r = r
        r.setdefault("sent", {"hours": {}, "fields": None, "title": None})
        r.setdefault("put", {"lastOkAt": 0, "lastTryAt": 0, "status": None, "code": None, "backoffUntil": 0,
                             "failures": 0, "dropped": [], "refusedTask": None, "refusedTitle": None})
        self.gate = False  # new step codes wait for a 200 from the sessions route in this process
        self.tried = False
        self.steps_refused_until = 0
        self.last_want = None
        self.last_send_at = 0
        self.quiet = False
        self.last_state = None
        self.uuid_session = bool(UUID_RE.match(session))

    def want(self, now, view, task):
        step = view["step"]
        if step in NEW_STEPS and (not self.gate or now < self.steps_refused_until):
            step = None
        want = {"state": "problem" if view["state"] == "problem" else
                "idle" if view["state"] == "idle" else "working", "taskId": task}
        if view["state"] == "problem":
            want["reason"] = view["reason"]
        elif step:
            want["step"] = step
        return want

    @staticmethod
    def core(want):
        return want["state"], want.get("taskId"), want.get("reason")

    def presence(self, now, view, task):
        """Sends presence when due; what was sent, or None."""
        want = self.want(now, view, task)
        quiet_after = {"idle": TIMES["quietIdle"], "waiting": TIMES["quietWaiting"]}.get(view["state"])
        reference = max(self.m.s.get("lastActivityAt") or 0, self.m.s.get("watchingSince") or 0)
        if quiet_after is not None and now - reference >= quiet_after:
            if not self.quiet:
                self.quiet_presence()
                self.quiet, self.last_want = True, None
            return None
        last = self.last_want
        due = (self.quiet or last is None or self.core(want) != self.core(last)
               or want != last and now - self.last_send_at >= TIMES["stepSpacing"]
               or now - self.last_send_at >= TIMES["presenceRepeat"])
        if not due:
            return None
        self.quiet = False
        status = self.send_presence(want)
        self.last_want, self.last_send_at = want, now
        if status == 422 and want.get("step") in NEW_STEPS:
            self.steps_refused_until = now + TIMES["routeBackoff"]
        return want

    def fields(self, now, view, task, ended_at=None):
        m = self.m
        started = m.started_at()
        last = max(m.activity_end(now) or started, started)
        put = self.r["put"]
        return {
            "claudeSessionId": self.sid,
            "host": self.host,
            "taskId": None if task and task == put.get("refusedTask") else task,
            "startedAt": iso(started),
            "lastActivityAt": iso(last),
            "endedAt": iso(max(ended_at, started)) if ended_at else None,
            "countedSince": iso(m.s["countedSince"]) if m.s.get("countedSince") else None,
            "model": m.model(),
            "contextTokens": min(int(m.s["context"]), 100_000_000) if m.s.get("context") else None,
            "contextLimit": m.context_limit() if m.s.get("context") or m.s.get("modelId") else None,
            "activeAgents": min(view["activeAgents"], 1000),
        }

    def changed_hours(self, now):
        m = self.m
        started = m.started_at()
        last = max(m.activity_end(now) or started, started)
        low, oldest = hour_of(started), now - KEEP_DAYS * 86400 + 3600
        sent = self.r["sent"]["hours"]
        out = []
        for h, v in sorted(m.buckets(now).items()):
            if h < low or h > last or h < oldest:
                continue
            v = [min(x, 10_000_000_000) for x in v[:4]] + [min(v[4], HOUR_MS)]
            old = sent.get(str(h))
            if old is None and not any(v) or old == v:
                continue
            if old and all(a <= b for a, b in zip(v, old)):
                continue  # the server keeps the greater value anyway
            out.append((h, v))
        return out[:MAX_HOURS]

    def session(self, now, view, task, ended_at=None, force=False):
        """Sends the row when due; the HTTP status, or None when nothing was due."""
        m, put = self.m, self.r["put"]
        if not self.uuid_session or m.started_at() is None:
            return None
        if now < put["backoffUntil"] and (not force or put["status"] in (401, 403, 404, 409)):
            return None
        fields = self.fields(now, view, task, ended_at)
        hours = self.changed_hours(now)
        title = m.s.get("title")
        if isinstance(title, str):
            # The route's 300 counts UTF-16 units, as JavaScript measures a string.
            title = title.strip().encode("utf-16-le")[:600].decode("utf-16-le", "ignore") or None
        send_title = title and title != self.r["sent"]["title"] and title != put.get("refusedTitle")
        changed = bool(hours) or fields != self.r["sent"]["fields"] or bool(send_title)
        settled = self.last_state not in (None, "idle") and view["state"] == "idle"
        self.last_state = view["state"]
        due = (force or not self.tried
               or changed and (now - put["lastTryAt"] >= TIMES["putSpacing"] or settled)
               or now - put["lastOkAt"] >= TIMES["keepalive"])
        if not due:
            return None
        self.tried = True
        body = dict(fields)
        if send_title:
            body["title"] = title
        if hours:
            body["hours"] = [{"hour": iso(h), "inputTokens": v[0], "outputTokens": v[1], "cacheReadTokens": v[2],
                              "cacheWriteTokens": v[3], "activeMs": v[4]} for h, v in hours]
        for field in put["dropped"]:
            body.pop(field, None)
        status, payload = self.put_session(presence_id(self.sid), body)
        put["lastTryAt"], put["status"] = now, status
        put["code"] = (payload or {}).get("code") if isinstance(payload, dict) else None
        if status == 200:
            self.gate = True
            put.update(lastOkAt=now, failures=0, backoffUntil=0)
            self.r["sent"]["fields"] = fields
            if "title" in body:
                self.r["sent"]["title"] = title
            for h, v in hours:
                self.r["sent"]["hours"][str(h)] = v
            return status
        named = refused_fields(payload)
        if status == 422 and put["code"] == "agent_field_forbidden" and named and named[0] not in put["dropped"]:
            put["dropped"].append(named[0])
            put["lastTryAt"] = 0
        elif status == 422 and ("taskId" in named and body.get("taskId") or "title" in named and "title" in body):
            if "taskId" in named and body.get("taskId"):
                put["refusedTask"] = body["taskId"]
            if "title" in named and "title" in body:
                put["refusedTitle"] = title
            put["lastTryAt"] = 0
        elif status in (401, 403, 404, 409):
            self.gate = False
            put["backoffUntil"] = now + TIMES["routeBackoff"]
        elif status in (422, 429):
            put["backoffUntil"] = now + TIMES["limitBackoff"]
        else:
            put["failures"] += 1
            put["backoffUntil"] = now + min(TIMES["retryMax"], TIMES["putSpacing"] * 2 ** (put["failures"] - 1))
        return status


def refused_fields(payload):
    """The body keys a 422 names: the guard's refusals and the controller's
    say `field`; a validation failure lists `messages` with paths such as
    hours.0.activeMs."""
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("field"), str):
        return [payload["field"]]
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    return [m["field"].split(".")[0] for m in messages if isinstance(m, dict) and isinstance(m.get("field"), str)]


def load_hook():
    return session_hook


def put_session(api, token, session_id, body):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"{api}/agents/sessions/{session_id}", data=json.dumps(body).encode(), method="PUT",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                          "Accept": "application/json", "User-Agent": "unio-session-watch/1"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        e.close()
        return e.code, payload
    except Exception:
        return 0, {}


def lower_priority():
    """Windows starts the watcher below normal priority instead (system.detach)."""
    if system.WINDOWS:
        return
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < 10:
            os.setpriority(os.PRIO_PROCESS, 0, 10)
    except (OSError, AttributeError):
        pass
    if not system.LINUX:
        return
    # Best effort at its lowest level rather than the idle class: on a machine at
    # load 15-40 (measured 2026-10-06) the idle class held a tick's few small writes
    # 11 s at p90 and 19 s at worst, against 1.8 s and 8 s, enough to miss the
    # page's 90 s presence window. Bulk reads, what idle would protect, are
    # bounded: a running session is joined at the end of its files (2 MB of the
    # main one, 256 KB of each recent agent's); only a fresh one is read whole.
    number = {"x86_64": 251, "aarch64": 30}.get(os.uname().machine)
    if number:
        try:
            import ctypes
            ctypes.CDLL(None, use_errno=True).syscall(number, 1, 0, 2 << 13 | 7)  # ioprio_set(self, best effort 7)
        except Exception:
            pass


def code_stamp():
    out = []
    for path in CODE:
        try:
            st = path.stat()
            out.append([st.st_ino, st.st_mtime_ns, st.st_size])
        except OSError:
            out.append(None)
    return out


def compiles():
    try:
        for path in CODE:
            compile(path.read_bytes(), str(path), "exec")
        return True
    except Exception:
        return False


class Watcher:
    def __init__(self, args, hook, argv=()):
        self.args, self.hook, self.session = args, hook, args.session
        self.argv = list(argv)
        self.state_path = hook.STATE / f"{args.session}.watch.json"
        self.binding_path = hook.STATE / f"{args.session}.json"
        self.stop = False
        self.code = code_stamp()
        self.code_refused = None
        self.task, self.binding_stamp = None, None
        now = time.time()
        saved = self.load()
        main = args.transcript or (saved or {}).get("transcript") or self.find_transcript()
        if saved:
            self.model = Model(main, saved.get("model"), now)
            if saved.get("pid") != args.pid or saved.get("pidStart") != args.pid_start:
                self.model.new_process()
                saved["watchingSince"] = now
            if not args.reexec:
                # A hook event started it, so the process runs this session now:
                # one it left for another and came back to is no ghost.
                saved["boundAt"] = now
            self.meta = saved
        else:
            self.model = Model(main, None, now)
            self.meta = {"boundAt": now, "watchingSince": now}
            exists = bool(main) and Path(main).exists()
            first = Model.first_stamp(Path(main)) if exists else None
            # A start whose own SessionStart hook timed out reaches the watcher
            # from a later event: a transcript begun minutes ago is still read whole.
            fresh = args.source in ("startup", "clear") or not exists or (first is not None and now - first < 600)
            self.model.adopt(fresh, now, args.model)
        self.meta.update(v=STATE_VERSION, session=args.session, pid=args.pid, pidStart=args.pid_start,
                         transcript=str(main) if main else None, ended=False, endedAt=None, imageAt=now,
                         watcherPid=os.getpid())
        self.meta.pop("model", None)
        self.meta.pop("endedBy", None)
        self.model.s["watchingSince"] = self.meta["watchingSince"]
        self.reporter = Reporter(self.model, args.session, self.send_presence, self.quiet_presence,
                                 self.put, socket.gethostname()[:64], self.meta.setdefault("report", {}))
        self.saved_at = 0
        self.ghost_at = 0
        self.forget_old_end(args.event_at or now)

    def forget_old_end(self, event_at):
        """An `ended` written before the event that started this watcher closed an
        earlier run of the session (it was resumed since). Left in place it would
        end this run at once, and the hook would skip the next real end as a repeat."""
        hook = self.hook
        with hook.Locked(self.session, wait=1) as ok:
            if not ok:
                return
            p = hook.read_presence(self.session)
            if (p.get("want") or {}).get("state") == "ended" and (p.get("requestedAt") or 0) < event_at:
                p.pop("want")
                p["seq"] = int(p.get("seq") or 0) + 1
                hook.write_presence(self.session, p)

    def load(self):
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("v") != STATE_VERSION or data.get("session") != self.session:
            return None
        return data

    def find_transcript(self):
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
        for path in root.glob(f"*/{self.session}.jsonl"):
            return str(path)
        return None

    def save(self, force=False):
        now = time.time()
        if not force and not self.model.dirty and now - self.saved_at < 60:
            return
        if not force and now - self.saved_at < TIMES["saveEvery"]:
            return
        data = dict(self.meta, model=self.model.dump(), savedAt=now)
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, separators=(",", ":")))
            system.replace(tmp, self.state_path)
            self.saved_at, self.model.dirty = now, False
        except OSError:
            pass

    def send_presence(self, want):
        hook = self.hook
        with hook.Locked(self.session, wait=1) as ok:
            if not ok:
                return None
            p = hook.read_presence(self.session)
            if (p.get("want") or {}).get("state") == "ended":
                return None
            seq = int(p.get("seq") or 0) + 1
            p.update(want=want, seq=seq, requestedAt=time.time())
            hook.write_presence(self.session, p)
        hook.send_presence(self.session, seq)
        return hook.read_presence(self.session).get("lastStatus")

    def quiet_presence(self):
        """Stops repeating: an old heartbeat's beat() and end_after_death() return
        early on an empty `want`, and the page drops the row after 90 s."""
        hook = self.hook
        with hook.Locked(self.session, wait=1) as ok:
            if not ok:
                return
            p = hook.read_presence(self.session)
            if (p.get("want") or {}).get("state") == "ended":
                return
            p.pop("want", None)
            p.update(seq=int(p.get("seq") or 0) + 1, requestedAt=time.time(), quietAt=time.time())
            hook.write_presence(self.session, p)

    def put(self, session_id, body):
        secret, api = self.hook.token(), self.hook.api()
        if not secret or not api:
            return 0, {}
        return put_session(api, secret, session_id, body)

    def binding(self):
        try:
            st = self.binding_path.stat()
            stamp_ = (st.st_mtime_ns, st.st_size)
        except OSError:
            self.task = None
            return None
        if stamp_ != self.binding_stamp:
            self.binding_stamp = stamp_
            try:
                self.task = (json.loads(self.binding_path.read_text()).get("task") or {}).get("id") or None
            except (OSError, ValueError, AttributeError):
                self.task = None
        return self.task

    def alive(self):
        return system.start_time(self.args.pid) == self.args.pid_start

    def ghost(self):
        """Another session id now runs in the same process (a /clear whose
        SessionEnd never ran): this one is over. One that ended, or whose
        watcher died, says nothing about where the process is now."""
        now = time.time()
        for path in self.hook.STATE.glob("*.watch.json"):
            if path == self.state_path:
                continue
            try:
                other = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if (isinstance(other, dict) and other.get("pid") == self.args.pid
                    and other.get("pidStart") == self.args.pid_start and not other.get("ended")
                    and now - (other.get("savedAt") or 0) < GHOST_STALE
                    and (other.get("boundAt") or 0) > self.meta["boundAt"]):
                return True
        return False

    def ended_by_hook(self):
        return (self.hook.read_presence(self.session).get("want") or {}).get("state") == "ended"

    def finish(self, why):
        now = time.time()
        if why != "session_end":
            try:
                self.hook.end_after_death(self.session)
            except Exception:
                pass
        view = self.model.view(now)
        for _ in range(3):
            status = self.reporter.session(now, view, self.binding(), ended_at=now, force=True)
            if status in (None, 200, 401, 403, 404, 409):
                break
            time.sleep(min(3.0, TIMES["tick"]))
        self.meta.update(ended=True, endedAt=now, endedBy=why)
        self.save(force=True)

    def maybe_reexec(self):
        if system.WINDOWS:
            return
        current = code_stamp()
        if current == self.code or current == self.code_refused:
            return
        if not compiles():
            self.code_refused = current
            return
        self.meta["execs"] = int(self.meta.get("execs") or 0) + 1
        self.save(force=True)
        argv = [a for a in self.argv if a != "--reexec"]
        os.execv(sys.executable, [sys.executable, "-I", str(ENTRY), "watch", *argv, "--reexec"])

    def tick(self):
        now = time.time()
        self.maybe_reexec()
        if self.model.main_path is None and now - self.model.listed_at >= TIMES["list"]:
            self.model.listed_at = now
            found = self.find_transcript()
            if found:
                self.model.set_main(found)
                self.meta["transcript"] = found
        if self.ended_by_hook():
            self.model.poll(now)
            self.finish("session_end")
            return False
        self.model.poll(now)
        continued = self.model.s.get("continuedAt")
        if continued and (self.model.s.get("lastActivityAt") or 0) <= continued:
            self.finish("continued")
            return False
        if not self.alive() and now - self.model.s["lastGrowthAt"] >= TIMES["endAfter"]:
            self.finish("process_gone")
            return False
        if now - self.ghost_at >= TIMES["ghostEvery"]:
            self.ghost_at = now
            if self.ghost():
                self.finish("cleared")
                return False
        task = self.binding()
        view = self.model.view(now)
        self.meta["view"] = view
        self.reporter.presence(now, view, task)
        self.reporter.session(now, view, task)
        if now - (self.meta.get("touchedAt") or 0) >= TIMES["keepalive"]:
            # The hook deletes state files untouched for 30 days; flock never touches the lock.
            self.meta["touchedAt"] = now
            try:
                os.utime(self.hook.STATE / f"{self.session}.watch.lock")
            except OSError:
                pass
        self.save()
        return True

    def run(self):
        signal.signal(signal.SIGTERM, lambda *a: setattr(self, "stop", True))
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        while not self.stop:
            try:
                if not self.tick():
                    return
            except Exception as e:
                self.meta["lastError"] = f"{type(e).__name__}: {e}"[:300]
                self.meta["lastErrorAt"] = time.time()
                self.model.dirty = True
                self.save()
            if not self.hook.STATE.is_dir():
                return
            deadline = time.monotonic() + TIMES["tick"]
            while not self.stop and time.monotonic() < deadline:
                time.sleep(min(0.2, TIMES["tick"]))
        self.save(force=True)


def main(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--session", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--pid-start", required=True)
    parser.add_argument("--transcript")
    parser.add_argument("--source", default="")
    parser.add_argument("--model")
    parser.add_argument("--event-at", type=float)
    parser.add_argument("--reexec", action="store_true")
    # Known ones only: a re-exec hands its flags to whatever version replaced this file.
    args = parser.parse_known_args(argv)[0]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.session) or os.environ.get("UNIO_INBOX_RUN"):
        return 0
    hook = load_hook()
    with hook.Locked(args.session, wait=0, name="watch") as free:
        if not free:
            return 0
        lower_priority()
        Watcher(args, hook, argv).run()
    return 0

