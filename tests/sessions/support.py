"""Shared by the session tests: the plugin's paths, the environment its
scripts get, synthetic transcripts, and the processes a test started.

Every test runs in a throwaway HOME, state and config folder against
fake_api.py on loopback. Transcripts are written by the Transcript class
below, never copied from a real session.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SCRIPTS = REPO / "plugins" / "unio" / "scripts"
ENTRY = SCRIPTS / "sessions.py"
LAUNCHER = SCRIPTS / "session-status.sh"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

from unio_sessions import system  # noqa: E402

WINDOWS = system.WINDOWS
# Loaded CI machines are slow to start processes; waits end early once their
# condition holds.
SLACK = float(os.environ.get("UNIO_TEST_SLACK") or 3)
# A hook run returns within this, Python's start included.
HOOK_LIMIT = 3.0 if WINDOWS else 1.5
# Windows refuses Python without these.
WINDOWS_VARS = ("SYSTEMROOT", "SystemRoot", "WINDIR", "PATHEXT", "COMSPEC", "TEMP", "TMP", "SystemDrive")


def posix_path():
    return os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def base_env(tmp, **extra):
    """What a hook gets in a throwaway HOME: its state, config and Claude Code
    folders all under `tmp`."""
    env = {k: os.environ[k] for k in WINDOWS_VARS if k in os.environ} if WINDOWS else {}
    env.update({
        "HOME": str(tmp),
        "USERPROFILE": str(tmp),
        "PATH": os.environ.get("PATH", "") if WINDOWS else posix_path(),
        "XDG_STATE_HOME": str(Path(tmp) / "state"),
        "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
        "LOCALAPPDATA": str(Path(tmp) / "localappdata"),
        "CLAUDE_CODE_EXECPATH": "/home/me/.local/share/claude/versions/2.1.287",
    })
    env.update(extra)
    return env


def presence_id(session):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"unio-session:{session}"))


def wait_for(test, check, limit, message):
    deadline = time.monotonic() + limit * SLACK
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            test.fail(message() if callable(message) else message)
        time.sleep(0.05)


def processes(*tokens):
    """Pids of this user's processes whose arguments hold `tokens` in a row.
    Linux reads /proc; macOS asks ps, which joins arguments with spaces.
    Windows has neither, so callers that need it skip there."""
    want = [t.encode() for t in tokens]
    found = []
    if sys.platform.startswith("linux"):
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                args = (proc / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if any(args[i:i + len(want)] == want for i in range(len(args))):
                found.append(int(proc.name))
        return found
    if WINDOWS:
        return []
    out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    joined = " ".join(tokens)
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if f" {joined} " in f" {command} ":
            if pid.isdigit() and int(pid) != os.getpid():
                found.append(int(pid))
    return found


def kill(pid):
    try:
        os.kill(pid, 15)  # SIGTERM; TerminateProcess on Windows
    except OSError:
        pass


def iso(t):
    from unio_sessions import watch
    return watch.iso(t)


def usage(i=0, o=0, cr=0, cw=0):
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}


class Transcript:
    """Appends Claude Code entries to one transcript file, in the shapes
    Claude Code 2.1.29x writes them."""

    def __init__(self, path, session, sidechain=False, agent=None):
        self.path, self.session, self.sidechain, self.agent = Path(path), session, sidechain, agent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def raw(self, text):
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def add(self, entry):
        entry = {"sessionId": self.session, "isSidechain": self.sidechain, **entry}
        if self.agent:
            entry["agentId"] = self.agent
        self.raw(json.dumps(entry) + "\n")
        return self

    def prompt(self, t, text="go", kind="human", meta=False, **extra):
        e = {"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": text}, **extra}
        if kind:
            e["origin"] = {"kind": kind}
        if meta:
            e["isMeta"] = True
        return self.add(e)

    def call(self, t, mid=None, use=None, stop="tool_use", tools=(), text=None, model="claude-opus-5-5"):
        """One assistant entry; `tools` are (id, name, input)."""
        self.n += 1
        content = [{"type": "tool_use", "id": tid, "name": name, "input": args} for tid, name, args in tools]
        if text is not None:
            content.insert(0, {"type": "text", "text": text})
        return self.add({"type": "assistant", "timestamp": iso(t), "requestId": f"req_{mid or self.n}",
                         "message": {"id": mid or f"msg_{self.path.stem}_{self.n}", "model": model,
                                     "role": "assistant", "stop_reason": stop, "content": content,
                                     "usage": use or usage()}})

    def result(self, t, tid, result=None, error=False, extra_text=None):
        content = [{"type": "tool_result", "tool_use_id": tid, "content": "ok", "is_error": error}]
        if extra_text:
            content.append({"type": "text", "text": extra_text})
        e = {"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": content}}
        if result is not None:
            e["toolUseResult"] = result
        return self.add(e)

    def synthetic(self, t, error, text):
        return self.add({"type": "assistant", "timestamp": iso(t), "isApiErrorMessage": True, "error": error,
                         "message": {"id": str(uuid.uuid4()), "model": "<synthetic>", "role": "assistant",
                                     "stop_reason": "stop_sequence", "content": [{"type": "text", "text": text}],
                                     "usage": usage()}})

    def system(self, t, subtype, **fields):
        return self.add({"type": "system", "subtype": subtype, "timestamp": iso(t), **fields})

    def notice(self, t, tid, status="completed", via="queue"):
        text = (f"<task-notification>\n<task-id>x</task-id>\n<tool-use-id>{tid}</tool-use-id>\n"
                f"<status>{status}</status>\n<summary>done</summary>\n</task-notification>")
        if via == "queue":
            return self.add({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(t), "content": text})
        if via == "attachment":
            return self.add({"type": "attachment", "timestamp": iso(t),
                             "attachment": {"type": "queued_command", "prompt": text}})
        return self.prompt(t, text, kind="task-notification")

    def model(self, t, model_id):
        return self.add({"type": "attachment", "timestamp": iso(t),
                         "attachment": {"type": "model", "identity": {"modelId": model_id}}})


def remove_tree(path):
    """A detached process may still hold a file open for a moment on Windows."""
    for _ in range(20):
        shutil.rmtree(path, ignore_errors=True)
        if not Path(path).exists():
            return
        time.sleep(0.25)
