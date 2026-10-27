"""Bind a Claude Code session to the Unio task it works on, and show it on the
agent's page in Unio.

One entry for five hook events (the event name arrives in the hook's stdin):

  UserPromptSubmit  a pasted Unio task link becomes the session's candidate task
  PostToolUse       get_task on the candidate, or the first create_task that is
                    not a follow-up, binds the task; later follow-ups are listed
                    beside it, except one created already started, which the
                    session moves on to: it becomes the task and the one before
                    goes to `earlier` (registered for the Unio tools only)
  SessionStart, Stop, SessionEnd  presence only (below)

The binding names no task to Claude: nothing a person typed in Unio reaches
Claude's context through a hook. It is kept for presence, the session row and
`unio resume`.

Presence: the session is one row on the agent's page (kind `session`, its id the
session id hashed to a UUID). Any event starts the session's watcher (watch.py)
when its lock is free: a detached process that lives as long as the Claude Code
process, reads the transcripts and reports what the session does (presence with
a step, and the session's row with its hourly usage) through the functions
below. While it runs, the hook reports nothing itself but `ended` on SessionEnd.
Without it (no Claude Code process above the hook) the hook reports alone: idle
on SessionStart and Stop, working with the bound task when a Unio tool runs or a
prompt is submitted in a bound session (repeated at most once a minute), ended
on SessionEnd. A detached process sends each state with a 3 s timeout, so the
hook never waits on the network; it carries no prompt text. Child sessions
(workflow agents) never use Unio and report nothing; neither do the inbox's own
runs (UNIO_INBOX_RUN), which are not coding sessions.

The token is read from the file UNIO_SESSION_TOKEN_FILE names, which
session-status.sh resolves for the session's folder, and the API is
UNIO_SESSION_API, which it reads from ~/.config/unio/api-url: neither the
token nor a folder ever reaches an argument or the environment.

State lives in the sessions folder (system.state_dir) as <session_id>.json,
with the same names and shapes as the hand-made hook these files replace, so a
session it already watches is never watched twice. The task is kept by UUID as
well as by key, because a key changes when the task changes team. Never blocks
or fails a turn: any error exits 0 silently.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from . import system

STATE = system.state_dir()
ENTRY = Path(__file__).resolve().parent.parent / "sessions.py"
LINK = re.compile(r"https?://(?:www\.)?unio\.so/app/tasks/([A-Za-z0-9]{2,5}-\d{1,6})\b")
TASK_URL = re.compile(r"/app/tasks/([A-Z0-9]{2,5}-\d{1,6})")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SESSION_ID = re.compile(r"[A-Za-z0-9._-]+")
TOKEN = re.compile(r"unio_[0-9A-Za-z]{46}")
# lib.sh's unio_api: https, or a loopback test API, then /v1.
API = re.compile(r"(https://[A-Za-z0-9.:/-]+|http://(127\.0\.0\.1|localhost):[A-Za-z0-9.:/-]*)/v1")
UNIO_TOOLS = ("mcp__unio__", "mcp__plugin_unio_unio__")
PRESENCE_REPEAT = 60
# The server stores a change of state or task at once, but a repeat of the
# same state at most once per 10 s, dropping the others; the extra second
# absorbs latency.
PRESENCE_SPACING = 11
PRESENCE_TIMEOUT = 3
# A send holds its turn for at most two PUTs.
SEND_WAIT = 2 * PRESENCE_TIMEOUT + 2

# Status values (names or categories) that mean work on a task begins now.
STARTED = {"in_progress", "started", "in progress", "em andamento"}


def api():
    value = os.environ.get("UNIO_SESSION_API") or ""
    return value if API.fullmatch(value) else None


def token():
    """The workspace's token, or None when its file holds anything else."""
    path = os.environ.get("UNIO_SESSION_TOKEN_FILE")
    if not path:
        return None
    try:
        value = Path(path).read_text(encoding="ascii", errors="replace").replace("\r", "").replace("\n", "")
    except OSError:
        return None
    return value if TOKEN.fullmatch(value) else None


def ready():
    return bool(api() and token())


def write_json(path, data):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    system.replace(tmp, path)


def load(session):
    path = STATE / f"{session}.json"
    try:
        return path, json.loads(path.read_text())
    except Exception:
        return path, {}


def save(path, data):
    STATE.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = int(time.time())
    write_json(path, data)


def result_text(response):
    """The tool result as one string, whatever shape the hook received it in."""
    if isinstance(response, str):
        return response
    try:
        return json.dumps(response)
    except Exception:
        return ""


def task_from(text):
    data = None
    try:
        data = json.loads(text)
    except Exception:
        pass
    # MCP results arrive as content blocks; the JSON payload is in their text.
    if isinstance(data, list):
        text = " ".join(b.get("text", "") for b in data if isinstance(b, dict)) or text
    elif isinstance(data, dict) and isinstance(data.get("content"), list):
        text = " ".join(b.get("text", "") for b in data["content"] if isinstance(b, dict)) or text
    try:
        payload = json.loads(text)
        task = payload.get("task", payload) if isinstance(payload, dict) else {}
        if isinstance(task, dict) and task.get("key") and task.get("id"):
            return {"id": task["id"], "key": task["key"], "url": task.get("url", "")}
    except Exception:
        pass
    m = TASK_URL.search(text)
    if not m:
        return None
    u = UUID.search(text)
    url = re.search(r"https?://[^\s\"']*/app/tasks/[^\s\"']+", text)
    return {"id": u.group(0) if u else "", "key": m.group(1), "url": url.group(0) if url else ""}


def bind(data, session):
    event = data.get("hook_event_name", "")
    if event not in ("UserPromptSubmit", "PostToolUse"):
        return
    # Tool hooks run in the background, so two can overlap.
    with Locked(session, wait=2, name="bind") as ok:
        if ok:
            bind_locked(data, session, event)


def bind_locked(data, session, event):
    path, state = load(session)

    if event == "UserPromptSubmit":
        m = LINK.search(data.get("prompt") or "")
        if m:
            state["candidate"] = m.group(1).upper()
            save(path, state)
        return

    tool = data.get("tool_name", "")
    args = data.get("tool_input") or {}
    task = task_from(result_text(data.get("tool_response")))
    if not task:
        return
    if tool.endswith("__create_task") and args.get("follow_up_of"):
        follow = state.setdefault("follow_ups", [])
        if task["key"] not in [f["key"] for f in follow]:
            follow.append(task)
        if str(args.get("status", "")).lower() in STARTED and state.get("task"):
            earlier = state.setdefault("earlier", [])
            if state["task"].get("id") not in [t.get("id") for t in earlier]:
                earlier.append(state["task"])
            state["task"] = task
    elif tool.endswith("__get_task") and state.get("candidate") == task["key"]:
        state["task"] = task
        state.pop("candidate", None)
    elif tool.endswith("__create_task") and not state.get("task"):
        state["task"] = task
    elif state.get("task") and state["task"].get("id") and state["task"]["id"] == task.get("id"):
        state["task"] = task  # same task, new key or url after a team or title change
    else:
        return
    save(path, state)


class Locked:
    """An exclusive lock on one of the session's lock files, given up after `wait` seconds."""

    def __init__(self, session, wait, name="presence"):
        self.path = STATE / f"{session}.{name}.lock"
        self.wait = wait
        self.file = None
        self.held = False

    def __enter__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a")
        deadline = time.monotonic() + self.wait
        while True:
            if system.try_lock(self.file):
                self.held = True
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)

    def __exit__(self, *exc):
        if self.held:
            system.unlock(self.file)
        self.file.close()


def presence_file(session):
    return STATE / f"{session}.presence.json"


def read_presence(session):
    try:
        data = json.loads(presence_file(session).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_presence(session, data):
    write_json(presence_file(session), data)


def wanted(data, session):
    """The state this event reports, or None when it reports nothing."""
    event = data.get("hook_event_name", "")
    task = (load(session)[1].get("task") or {}).get("id") or None
    if event in ("SessionStart", "Stop"):
        return {"state": "idle"}
    if event == "SessionEnd":
        return {"state": "ended"}
    if event == "PostToolUse" and str(data.get("tool_name", "")).startswith(UNIO_TOOLS):
        return {"state": "working", "taskId": task}
    if event == "UserPromptSubmit" and task:
        return {"state": "working", "taskId": task}
    return None


_claude = []


def claude_process():
    if not _claude:
        _claude.append(system.claude_process())
    return _claude[0]


def is_child_session():
    found = claude_process()
    return bool(found) and system.is_child(found[0])


def ended_already(data, session):
    """Background hooks may land after SessionEnd: only a new start reopens
    an ended session."""
    if data.get("hook_event_name") in ("SessionStart", "SessionEnd"):
        return False
    return (read_presence(session).get("want") or {}).get("state") == "ended"


def report_presence(data, session):
    if not ready() or is_child_session() or ended_already(data, session):
        return
    if data.get("hook_event_name") != "SessionEnd" and start_watcher(data, session):
        return
    want = wanted(data, session)
    if want:
        request_presence(session, want)


def request_presence(session, want):
    with Locked(session, wait=1) as ok:
        if not ok:
            return
        p = read_presence(session)
        now = time.time()
        if p.get("want") == want and (want["state"] == "ended" or now - p.get("requestedAt", 0) < PRESENCE_REPEAT):
            return
        seq = int(p.get("seq") or 0) + 1
        p.update(want=want, seq=seq, requestedAt=now)
        write_presence(session, p)
    system.detach(["-I", str(ENTRY), "send-presence", session, str(seq)])


def start_watcher(data, session):
    """True when the session's watcher runs (or was just started) and reports
    presence in the hook's place."""
    # A hook run by hand from a Claude Code shell sits under that other session.
    own = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if own and own != session:
        return False
    found = claude_process()
    if not found:
        return False
    with Locked(session, wait=0, name="watch") as free:
        if not free:
            return True
    args = ["-I", str(ENTRY), "watch", "--session", session, "--pid", str(found[0]), "--pid-start", found[1],
            "--event-at", f"{time.time():.3f}"]
    if isinstance(data.get("transcript_path"), str) and data["transcript_path"]:
        args += ["--transcript", data["transcript_path"]]
    if data.get("hook_event_name") == "SessionStart":
        args += ["--source", str(data.get("source") or "")]
        if isinstance(data.get("model"), str):
            args += ["--model", data["model"]]
    system.detach(args, background=True)
    return True


def client():
    # Claude Code runs from ~/.local/share/claude/versions/<version>; its hooks inherit the path.
    version = re.split(r"[\\/]", os.environ.get("CLAUDE_CODE_EXECPATH") or "")[-1]
    if version.lower().endswith(".exe"):
        version = version[:-4]
    return "Claude Code" + (f" {version}" if re.fullmatch(r"\d+\.\d+\.\d+", version) else "")


def put(presence_id, body, secret):
    # Imported here: urllib alone is about 0.1 s of every hook run's start.
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"{api()}/agents/presence/{presence_id}", data=json.dumps(body).encode(),
                                 method="PUT",
                                 headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json",
                                          "Accept": "application/json", "User-Agent": "unio-session/1"})
    try:
        with urllib.request.urlopen(req, timeout=PRESENCE_TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code
    except Exception:
        return 0


def deliver(session, want):
    """One state to the server: its HTTP status, or 0 when it never answered."""
    import socket
    import uuid
    secret = token()
    if not secret or not api():
        return 0
    body = {"kind": "session", "host": socket.gethostname()[:64], "client": client(),
            **{k: v for k, v in want.items() if v is not None}}
    presence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"unio-session:{session}"))
    status = put(presence_id, body, secret)
    if status == 422 and "taskId" in body:
        # A task outside the agent's teams: the session still shows, without it.
        body.pop("taskId")
        status = put(presence_id, body, secret)
    return status


def record(session, status, seq):
    with Locked(session, wait=2) as ok:
        if ok:
            p = read_presence(session)
            p.update(lastStatus=status, lastSentAt=int(time.time()), lastSeq=seq)
            write_presence(session, p)


def send_presence(session, seq):
    """The detached half: sends the latest state at once, unless a newer event has
    taken over the sending. Sends take turns, so an older state never lands after
    a newer one; the state last sent, repeated within PRESENCE_SPACING, is not
    sent again, since the server would drop it."""
    with Locked(session, wait=SEND_WAIT, name="send") as sending:
        if not sending:
            return
        with Locked(session, wait=2) as ok:
            if not ok:
                return
            p = read_presence(session)
            if p.get("seq") != seq:
                return
            want = p["want"]
            if want == p.get("sent") and time.time() - p.get("sentAt", 0) < PRESENCE_SPACING:
                p["lastSeq"] = seq
                write_presence(session, p)
                return
            p.update(sent=want, sentAt=time.time())
            write_presence(session, p)
        record(session, deliver(session, want), seq)


def end_after_death(session):
    """Claude Code died without a SessionEnd: say so rather than leave the
    session working on the page until it ages out. A quiet session (no
    `want`) has already dropped off it."""
    with Locked(session, wait=2) as ok:
        if not ok:
            return
        p = read_presence(session)
        if not p.get("want") or p["want"].get("state") == "ended":
            return
        seq = int(p.get("seq") or 0) + 1
        p.update(want={"state": "ended"}, seq=seq, requestedAt=time.time())
        write_presence(session, p)
    send_presence(session, seq)


def forget_old_files():
    """Sessions older than 30 days are of no use to anyone. A watcher's lock
    is open, and Windows refuses to delete it."""
    cutoff = time.time() - 30 * 86400
    for f in [*STATE.glob("*.json"), *STATE.glob("*.lock")]:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def main(stdin):
    try:
        data = json.loads(stdin.read() or "{}")
        if os.environ.get("UNIO_INBOX_RUN") or not isinstance(data, dict):
            return 0
        session = data.get("session_id") or ""
        if isinstance(session, str) and SESSION_ID.fullmatch(session):
            for step in (bind, report_presence):
                try:
                    step(data, session)
                except Exception:
                    pass
        forget_old_files()
    except Exception:
        pass
    return 0


def main_send(argv):
    try:
        if len(argv) == 2 and SESSION_ID.fullmatch(argv[0]):
            send_presence(argv[0], int(argv[1]))
    except Exception:
        pass
    return 0


