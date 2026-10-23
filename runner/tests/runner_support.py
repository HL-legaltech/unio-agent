"""Shared pieces of unio-runner's tests: the script loaded as a module, a
throwaway home with fake programs on PATH, and a fake Unio API.

unio-runner always runs the real runner/unio-inbox beside it, for
--print-paths, --check and the service itself. The fakes stand in for the
rest, so no test reaches a real service manager, a real Claude Code or the
network:

- the Unio API: GET /v1/agents/me by token, and an empty inbox and presence
  for a runner the fake service managers start;
- claude: --version and auth status --json;
- systemctl, loginctl, launchctl, plutil, schtasks and powershell: each call
  is logged; one that starts a workspace's service starts the real
  unio-inbox in the background as the service would, and one that stops it
  ends that process.

CI also renders the service definitions with this file, to check them with
each system's own tool:

    python runner/tests/runner_support.py render systemd|launchd|schtasks <file>
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

RUNNER_DIR = Path(__file__).resolve().parent.parent
UNIO_RUNNER = RUNNER_DIR / "unio-runner"


def load_runner():
    loader = importlib.machinery.SourceFileLoader("unio_runner", str(UNIO_RUNNER))
    spec = importlib.util.spec_from_loader("unio_runner", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def fake_token(text):
    """A fake token in the real shape, unio_ and 46 base62 characters."""
    return "unio_" + (text * 4)[:46]


RUNNER = fake_token("RunnerFakeTokenNotASecret")
SESSION = fake_token("SessionFakeTokenNotASecret")
OTHER = fake_token("OtherWorkspaceFakeTokenNotASecret")
REVOKED = fake_token("RevokedFakeTokenNotASecret")
OFF = fake_token("AgentsOffFakeTokenNotASecret")
BARRED = fake_token("BarredAgentFakeTokenNotASecret")
TOKENS = (RUNNER, SESSION, OTHER, REVOKED, OFF, BARRED)

WORKSPACE = "WorkspaceNameMarker"
AGENT = "AgentNameMarker"
TEAMS = [{"id": 1, "name": "Platform", "identifier": "PLA"}, {"id": 2, "name": "Support", "identifier": "SUP"}]


def me_body(org, purpose):
    return {"data": {
        "agent": {"id": 7, "clerkId": "user_fake", "name": AGENT},
        "writing": {"language": "pt-BR"},
        "workspace": {"clerkId": org, "name": WORKSPACE, "sigla": "WNM", "appUrl": "https://unio.so"},
        "teams": TEAMS,
        "token": {"id": 3, "name": "Runner", "prefix": "unio_", "purpose": purpose,
                  "expiresAt": "2027-01-01T00:00:00.000Z", "expiresInDays": 70},
    }}


class FakeApi:
    """GET /v1/agents/me on 127.0.0.1, answering by bearer token, and for a
    runner token the empty inbox and the presence a running unio-inbox calls.
    Each request is recorded as (method, path), so a test can prove a token
    never reached a URL and count the calls."""

    ANSWERS = {
        RUNNER: (200, me_body("org_test", "runner")),
        SESSION: (200, me_body("org_test", "session")),
        OTHER: (200, me_body("org_Other", "runner")),
        REVOKED: (401, {"error": "Token inválido.", "code": "invalid_token"}),
        OFF: (403, {"error": "Agentes desligados.", "code": "agents_disabled"}),
        BARRED: (403, {"error": "Sem permissão.", "code": "forbidden"}),
    }

    def __init__(self):
        self.requests = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self):
                api.requests.append((self.command, self.path))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                token = (self.headers.get("Authorization") or "")[len("Bearer "):]
                status, body = api.ANSWERS.get(token, (401, {"error": "Token inválido."}))
                route = (self.command, self.path.split("?", 1)[0])
                if route == ("GET", "/v1/agents/me"):
                    pass
                elif status == 200 and route == ("GET", "/v1/agents/inbox"):
                    body = {"data": []}
                elif status == 200 and route[0] == "PUT" and route[1].startswith("/v1/agents/presence/"):
                    body = {"data": {}}
                else:
                    status, body = 404, {"error": "Não encontrado."}
                data = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_port_url():
    """An API address where nothing answers."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return "http://127.0.0.1:%d/v1" % s.getsockname()[1]


FAKE_CLAUDE = r'''"""The fake claude of unio-runner's tests: FAKE_CLAUDE_AUTH is in (the
default), out, or old for a CLI without auth status."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
auth = os.environ.get("FAKE_CLAUDE_AUTH", "in")
if args == ["--version"]:
    print("2.1.309 (Claude Code)")
    sys.exit(0)
if args == ["auth", "status", "--json"] and auth != "old":
    signed = auth == "in"
    print(json.dumps({"loggedIn": signed, "authMethod": "claude.ai" if signed else "none",
                      "apiProvider": "firstParty"}, indent=2))
    sys.exit(0 if signed else 1)
print("error: unknown command " + " ".join(args), file=sys.stderr)
sys.exit(1)
'''

FAKE_SERVICE = r'''"""The fake service managers of unio-runner's tests (see runner_support.py).
FAKE_SERVICE_STATE holds each workspace's state (running, ready: installed
and not running, stopped or disabled) and the pid of the runner it started.
Starting a service starts the real unio-inbox FAKE_SERVICE_INBOX names, in
the background as the service would, and waits for its start line; ending
one stops that process. A task's state, as Task Scheduler's, is whether
that process still runs, which it may stop on its own (its stop file), and
/Run while it runs starts no second one (IgnoreNew). `cleanup` ends every
runner still running."""
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

tool, args = sys.argv[1], sys.argv[2:]
with open(os.environ["FAKE_SERVICE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps([tool] + args) + "\n")
INBOX = os.environ["FAKE_SERVICE_INBOX"]
WAIT = 60
state_file = Path(os.environ["FAKE_SERVICE_STATE"])
try:
    saved = json.loads(state_file.read_text(encoding="utf-8"))
except (OSError, ValueError):
    saved = {}
state = saved.get("services") or {}
pids = saved.get("pids") or {}


def save():
    state_file.write_text(json.dumps({"services": state, "pids": pids}), encoding="utf-8")


def org_in(text):
    found = re.search(r"org_[A-Za-z0-9]+", text)
    return found.group(0) if found else None


def log_file(org):
    """The runner's log, where unio-inbox --print-paths puts it."""
    done = subprocess.run([sys.executable, INBOX, "--workspace", org, "--print-paths"], stdin=subprocess.DEVNULL,
                          capture_output=True, encoding="utf-8", check=True)
    return Path(json.loads(done.stdout)["state"]) / "log.jsonl"


def size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def logged_after(path, offset, event):
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            lines = f.read().splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            if json.loads(line).get("event") == event:
                return True
        except (ValueError, AttributeError):
            continue
    return False


def wait_for(path, offset, event, running):
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline and running() and not logged_after(path, offset, event):
        time.sleep(0.2)


def ours(pid):
    """Whether pid is still a runner this fake started: on Linux its command
    line names the runner (a reused pid, or a zombie, does not); elsewhere
    the pid recorded moments ago is trusted."""
    if not os.path.isdir("/proc/self"):
        return True
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return os.fsencode(INBOX) in f.read()
    except OSError:
        return False


def alive(pid):
    if pid is None:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) would end the process on Windows.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return ours(pid)


def start(org):
    end(org)
    log = log_file(org)
    mark = size(log)
    # A test's runner must never update the checkout the tests run from.
    env = dict(os.environ, PYTHONUTF8="1", UNIO_RUNNER_AUTOUPDATE="off")
    if os.name == "nt":
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    else:
        flags = {"start_new_session": True}
    child = subprocess.Popen([sys.executable, INBOX, "--workspace", org], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, **flags)
    state[org] = "running"
    pids[org] = child.pid
    save()
    wait_for(log, mark, "start", lambda: child.poll() is None)


def end(org):
    pid = pids.pop(org, None)
    save()
    if not alive(pid):
        return
    log = log_file(org)
    mark = size(log)
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:
            ctypes.windll.kernel32.WaitForSingleObject(handle, WAIT * 1000)
            ctypes.windll.kernel32.CloseHandle(handle)
        return
    if not ours(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    wait_for(log, mark, "stop", lambda: alive(pid))


def finish(code=0, out=None):
    if out is not None:
        print(out)
    sys.exit(code)


org = org_in(" ".join(args))
if tool == "cleanup":
    for name in list(pids):
        end(name)
    finish()
if tool == "systemctl":
    args = [a for a in args if a != "--user"]
    verb = args[0]
    if verb == "show-environment":
        finish(0, "PATH=/usr/bin:/bin")
    if verb in ("daemon-reload", "reset-failed"):
        finish()
    if verb == "enable":
        if "--now" in args:
            start(org)
        finish()
    if verb == "restart":
        start(org)
        finish()
    if verb == "disable":
        if "--now" in args and org in state:
            end(org)
            state[org] = "stopped"
            save()
        finish()
    if verb == "is-active":
        running = state.get(org) == "running"
        finish(0 if running else 3, "active" if running else "inactive")
if tool == "loginctl":
    finish(0, "no")
if tool == "plutil":
    finish(0, args[-1] + ": OK")
if tool == "launchctl":
    verb = args[0]
    if verb in ("bootstrap", "kickstart"):
        start(org)
        finish()
    if verb == "bootout":
        if state.get(org) != "running":
            finish(3, "Boot-out failed: 3: No such process")
        end(org)
        state[org] = "stopped"
        save()
        finish()
    if verb in ("enable", "disable"):
        finish()
    if verb == "print":
        if state.get(org) != "running":
            finish(113, "Could not find service")
        finish(0, "%s = {\n\tstate = running\n}" % args[-1])
if tool == "schtasks":
    verb = args[0].lower()
    if verb == "/create":
        state[org] = "ready"
        save()
        finish(0, "SUCCESS: The scheduled task was successfully created.")
    if org not in state:
        finish(1, "ERROR: The system cannot find the file specified.")
    if verb == "/run":
        if state[org] == "disabled":
            finish(1, "ERROR: The task is disabled.")
        if not alive(pids.get(org)):
            start(org)
        finish(0, "SUCCESS: Attempted to run the scheduled task.")
    if verb == "/end":
        end(org)
        state[org] = "ready" if state[org] == "running" else state[org]
        save()
        finish()
    if verb == "/change":
        # Neither starts nor stops the runner, as in Task Scheduler.
        state[org] = "disabled" if "/DISABLE" in args else "ready"
        save()
        finish()
    if verb == "/delete":
        end(org)
        del state[org]
        save()
        finish()
    if verb == "/query":
        finish()
if tool == "powershell":
    if org not in state:
        finish(0, "-1")
    finish(0, "4" if alive(pids.get(org)) else "1" if state[org] == "disabled" else "3")
print("unexpected %s call: %s" % (tool, " ".join(args)), file=sys.stderr)
sys.exit(64)
'''

SERVICE_TOOLS = ("systemctl", "loginctl", "launchctl", "plutil", "schtasks", "powershell")
# Variables of the environment the tests run in that would change what the
# runner reads, or tie it to a Claude Code session.
DROPPED = ("UNIO_", "XDG_", "CLAUDE", "FAKE_", "ANTHROPIC_", "GIT_", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def write_program(folder, name, script, *args):
    """A command named `name` in `folder` that runs the Python file `script`."""
    if os.name == "nt":
        path = folder / (name + ".cmd")
        line = " ".join('"%s"' % a for a in (sys.executable, script) + args)
        path.write_text("@echo off\r\n%s %%*\r\n" % line, encoding="utf-8")
    else:
        path = folder / name
        line = " ".join("'%s'" % a for a in (sys.executable, script) + args)
        path.write_text('#!/bin/sh\nexec %s "$@"\n' % line, encoding="utf-8")
        path.chmod(0o755)
    return path


class Sandbox:
    """A throwaway home with the fakes on PATH, removed when the test ends."""

    def __init__(self, test):
        self.root = Path(tempfile.mkdtemp(prefix="unio-runner-tests-")).resolve()
        test.addCleanup(shutil.rmtree, str(self.root), True)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.lib = self.root / "lib"
        for folder in (self.home, self.bin, self.lib, self.root / "appdata", self.root / "localappdata"):
            folder.mkdir()
        self.inbox = RUNNER_DIR / "unio-inbox"
        (self.lib / "claude.py").write_text(FAKE_CLAUDE, encoding="utf-8")
        (self.lib / "service.py").write_text(FAKE_SERVICE, encoding="utf-8")
        write_program(self.bin, "claude", str(self.lib / "claude.py"))
        for tool in SERVICE_TOOLS:
            write_program(self.bin, tool, str(self.lib / "service.py"), tool)
        self.claude_log = self.root / "claude.log"
        self.service_log = self.root / "service.log"
        self.service_state = self.root / "service-state.json"
        self.extra = {}
        # Runs before the folder goes: a runner a fake service started writes in it.
        test.addCleanup(self.stop_services)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(DROPPED)}
        env.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_SERVICE_INBOX": str(self.inbox),
            "FAKE_CLAUDE_LOG": str(self.claude_log),
            "FAKE_SERVICE_LOG": str(self.service_log),
            "FAKE_SERVICE_STATE": str(self.service_state),
            "PYTHONIOENCODING": "utf-8",
            "NO_PROXY": "127.0.0.1,localhost",
        })
        env.update(self.extra)
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def runner(self, args, input_text="", **extra):
        """unio-runner run as a person runs it; returns (exit code, output)."""
        done = subprocess.run(
            [sys.executable, str(UNIO_RUNNER)] + list(args), input=input_text, capture_output=True,
            encoding="utf-8", errors="replace", env=self.env(**extra), timeout=180)
        return done.returncode, done.stdout + done.stderr

    def stop_services(self):
        """Ends every runner a fake service manager left running."""
        if self.service_state.exists():
            subprocess.run([sys.executable, str(self.lib / "service.py"), "cleanup"], stdin=subprocess.DEVNULL,
                           capture_output=True, env=self.env(), timeout=180)

    @contextlib.contextmanager
    def inside(self, **extra):
        """This process's environment as the sandbox's, for in-process tests."""
        with mock.patch.dict(os.environ, self.env(**extra), clear=True):
            yield

    def logged(self, path):
        try:
            return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
        except OSError:
            return []

    def paths(self, org="org_test"):
        if os.name == "nt":
            config = self.root / "appdata" / "unio" / "runners" / org
            state = self.root / "localappdata" / "unio" / "runners" / org
        else:
            config = self.home / ".config" / "unio" / "runners" / org
            state = self.home / ".local" / "state" / "unio" / "runners" / org
        return config, state, config / "runner-token"


class Recorder:
    """A stand-in for unio-runner's run(): records each argv and answers from
    `answers`, keyed by an argv prefix (the longest match wins)."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv, env=None, timeout=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for prefix in sorted(self.answers, key=len, reverse=True):
            if tuple(argv[:len(prefix)]) == tuple(prefix):
                return self.answers[prefix]
        return 0, "", ""


@contextlib.contextmanager
def captured():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        yield out


def render(kind, out):
    ur = load_runner()
    out = Path(out)
    inbox = str(ur.HERE / "unio-inbox")
    # As setup run with XDG folders writes it, a space in one to quote.
    env = {"XDG_CONFIG_HOME": str(out.parent / "xdg config"), "XDG_STATE_HOME": str(out.parent / "xdg-state")}
    if kind == "systemd":
        data = ur.render_systemd(sys.executable, inbox, ur.service_path(), env).encode("utf-8")
    elif kind == "launchd":
        data = ur.render_launchd("org_ci", sys.executable, inbox, ur.service_path(),
                                 str(out.with_name("service.log")), env).encode("utf-8")
    elif kind == "schtasks":
        data = ur.render_task("org_ci", ur.service_python("schtasks"), inbox, ur.task_user(),
                              str(out.parent), datetime.now()).encode("utf-16")
    else:
        sys.exit("render systemd|launchd|schtasks <file>")
    out.write_bytes(data)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "render":
        render(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
