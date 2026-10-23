"""Shared pieces of unio-runner's tests: the script loaded as a module, a
throwaway home with fake programs on PATH, and a fake Unio API.

The fakes stand in for what unio-runner calls, so no test reaches a real
service manager, a real Claude Code or the network:

- unio-inbox: --print-paths and --check exactly as the stage 6 contract
  defines them, and a service run that logs a start line and exits, so
  integrating the real runner is a drop-in;
- claude: --version and auth status --json;
- systemctl, loginctl, launchctl, plutil, schtasks and powershell: each call
  is logged, and one that starts a workspace's service runs the fake
  unio-inbox as the service would.

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
TOKENS = (RUNNER, SESSION, OTHER, REVOKED, OFF)

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
    """GET /v1/agents/me on 127.0.0.1, answering by bearer token. Each request
    is recorded as (method, path), so a test can prove a token never reached a
    URL and count the calls."""

    ANSWERS = {
        RUNNER: (200, me_body("org_test", "runner")),
        SESSION: (200, me_body("org_test", "session")),
        OTHER: (200, me_body("org_Other", "runner")),
        REVOKED: (401, {"error": "Token inválido.", "code": "invalid_token"}),
        OFF: (403, {"error": "Agentes desligados.", "code": "agents_disabled"}),
    }

    def __init__(self):
        self.requests = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                api.requests.append(("GET", self.path))
                token = (self.headers.get("Authorization") or "")[len("Bearer "):]
                status, body = api.ANSWERS.get(token, (401, {"error": "Token inválido."}))
                if self.path != "/v1/agents/me":
                    status, body = 404, {"error": "Não encontrado."}
                data = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

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


FAKE_INBOX = r'''"""The fake unio-inbox of unio-runner's tests (see runner_support.py)."""
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def paths(org):
    if os.name == "nt":
        config = Path(os.environ["APPDATA"]) / "unio" / "runners" / org
        state = Path(os.environ["LOCALAPPDATA"]) / "unio" / "runners" / org
    else:
        home = Path.home()
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "unio" / "runners" / org
        state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state") / "unio" / "runners" / org
    return config, state, config / "runner-token"


def setting(config, key):
    if os.environ.get(key):
        return os.environ[key]
    try:
        lines = (config / "env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip()
    return None


def check(org):
    config, _, token_file = paths(org)
    problems, workspace, agent, language = [], None, None, None
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        problems.append("the profile has no runner-token")
    else:
        api = (setting(config, "UNIO_API_URL") or "https://api.unio.so/v1").rstrip("/")
        request = urllib.request.Request(api + "/agents/me", headers={"Authorization": "Bearer " + token})
        data = None
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                data = json.loads(reply.read().decode("utf-8"))["data"]
        except urllib.error.HTTPError as e:
            problems.append("Unio answered HTTP %d" % e.code)
        except (urllib.error.URLError, OSError):
            problems.append("Unio did not answer")
        if data:
            workspace = {"clerkId": data["workspace"]["clerkId"], "name": data["workspace"]["name"]}
            agent = {"name": data["agent"]["name"]}
            language = data["writing"]["language"]
            if data["token"]["purpose"] != "runner":
                problems.append("not a runner token")
            if workspace["clerkId"] != org:
                problems.append("the token belongs to another workspace")
    if not shutil.which("claude"):
        problems.append("claude is not on PATH")
    print(json.dumps({"ok": not problems, "workspace": workspace, "agent": agent, "language": language,
                      "problems": problems}))
    return 0 if not problems else 1


def main(argv):
    log = os.environ.get("FAKE_INBOX_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\n")
    if len(argv) < 2 or argv[0] != "--workspace" or not re.match(r"^org_[A-Za-z0-9]+$", argv[1]):
        print("usage: unio-inbox --workspace org_<id> [--print-paths | --check]", file=sys.stderr)
        return 2
    org, rest = argv[1], argv[2:]
    if rest == ["--print-paths"]:
        config, state, token = paths(org)
        print(json.dumps({"config": str(config), "state": str(state), "token": str(token)}))
        return 0
    if rest == ["--check"]:
        return check(org)
    if rest:
        return 2
    _, state, _ = paths(org)
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": "start",
                            "pid": os.getpid(), "version": "2.0.0"}) + "\n")
    return 0


sys.exit(main(sys.argv[1:]))
'''

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
FAKE_SERVICE_STATE holds each workspace's state: running, ready (installed,
not running), stopped or disabled."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

tool, args = sys.argv[1], sys.argv[2:]
with open(os.environ["FAKE_SERVICE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps([tool] + args) + "\n")
state_file = Path(os.environ["FAKE_SERVICE_STATE"])
try:
    state = json.loads(state_file.read_text(encoding="utf-8"))
except (OSError, ValueError):
    state = {}


def save():
    state_file.write_text(json.dumps(state), encoding="utf-8")


def org_in(text):
    found = re.search(r"org_[A-Za-z0-9]+", text)
    return found.group(0) if found else None


def start(org):
    state[org] = "running"
    save()
    subprocess.run([sys.executable, os.environ["UNIO_RUNNER_INBOX"], "--workspace", org], check=False)


def finish(code=0, out=None):
    if out is not None:
        print(out)
    sys.exit(code)


org = org_in(" ".join(args))
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
        start(org)
        finish()
    if verb == "/end":
        state[org] = "ready" if state[org] == "running" else state[org]
        save()
        finish()
    if verb == "/change":
        state[org] = "disabled" if "/DISABLE" in args else "ready"
        save()
        finish()
    if verb == "/delete":
        del state[org]
        save()
        finish()
    if verb == "/query":
        finish()
if tool == "powershell":
    finish(0, str({"running": 4, "ready": 3, "disabled": 1}.get(state.get(org), -1)))
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
        self.inbox = self.lib / "unio-inbox"
        self.inbox.write_text(FAKE_INBOX, encoding="utf-8")
        (self.lib / "claude.py").write_text(FAKE_CLAUDE, encoding="utf-8")
        (self.lib / "service.py").write_text(FAKE_SERVICE, encoding="utf-8")
        write_program(self.bin, "claude", str(self.lib / "claude.py"))
        for tool in SERVICE_TOOLS:
            write_program(self.bin, tool, str(self.lib / "service.py"), tool)
        self.inbox_log = self.root / "inbox.log"
        self.claude_log = self.root / "claude.log"
        self.service_log = self.root / "service.log"
        self.service_state = self.root / "service-state.json"
        self.extra = {}

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(DROPPED)}
        env.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "UNIO_RUNNER_INBOX": str(self.inbox),
            "FAKE_INBOX_LOG": str(self.inbox_log),
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
    if kind == "systemd":
        data = ur.render_systemd(sys.executable, inbox, ur.service_path()).encode("utf-8")
    elif kind == "launchd":
        data = ur.render_launchd("org_ci", sys.executable, inbox, ur.service_path(),
                                 str(out.with_name("service.log"))).encode("utf-8")
    elif kind == "schtasks":
        data = ur.render_task("org_ci", ur.service_python("schtasks"), inbox, ur.task_user(),
                              str(out.parent)).encode("utf-16")
    else:
        sys.exit("render systemd|launchd|schtasks <file>")
    out.write_bytes(data)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "render":
        render(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
