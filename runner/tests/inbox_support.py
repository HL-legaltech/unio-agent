"""What the runner's tests share: unio-inbox loaded as a module, a throwaway
home for each test, and a fake Unio API on 127.0.0.1.

Stdlib only, like the runner, so the tests run on any Python 3.9 or later
on Linux, macOS and Windows: python -m unittest discover -s runner/tests
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

# unio-inbox has no .py suffix, so a cache file would land as runner/__pycache__/unio-inbox.*.
sys.dont_write_bytecode = True

RUNNER_DIR = Path(__file__).resolve().parent.parent
RUNNER = RUNNER_DIR / "unio-inbox"


def load_runner():
    loader = importlib.machinery.SourceFileLoader("unio_inbox", str(RUNNER))
    spec = importlib.util.spec_from_loader("unio_inbox", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


inbox = load_runner()


class HomeCase(unittest.TestCase):
    """A test with a home of its own: HOME, USERPROFILE and the XDG and
    Windows folders point inside it, and CLAUDE_CONFIG_DIR too, so nothing
    reads or writes the real ones."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unio-inbox-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "UNIO_RUNNER_AUTOUPDATE": "off",
        }
        cleared = {k: "" for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        patcher = mock.patch.dict(os.environ, {**cleared, **self.env})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in cleared:
            os.environ.pop(key, None)

    def run_inbox(self, *args, env=None, timeout=60):
        """The runner as a service manager starts it: a process of its own."""
        return subprocess.run([sys.executable, str(RUNNER), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env or dict(os.environ), timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def profile(self, workspace="org_test", token=None, repos=None):
        """A profile's configuration folder, with a runner token and repos.json when given."""
        config = Path(inbox.profile_dirs(workspace)[0])
        config.mkdir(parents=True, exist_ok=True)
        if token is not None:
            (config / "runner-token").write_text(token + "\n", encoding="utf-8")
        if repos is not None:
            (config / "repos.json").write_text(json.dumps(repos), encoding="utf-8")
        return config


class FakeUnio:
    """Unio's API as the runner calls it, served from a thread on 127.0.0.1.
    GET /v1/agents/me answers by bearer token: RUNNER is a runner token of
    workspace org_test, SESSION a person's token there, OTHER a runner
    token of another workspace and OFF one whose workspace has agents
    switched off; any other token gets 401. Every request is kept in
    `requests` as {method, path, query, body, headers}."""

    RUNNER = "unio_rt_test_runner_token"
    SESSION = "unio_at_test_session_token"
    OTHER = "unio_rt_test_other_workspace"
    OFF = "unio_rt_test_agents_off"

    def __init__(self, workspace="org_test", language="pt-BR"):
        self.workspace = workspace
        self.language = language
        self.teams = [{"id": 1, "name": "Product", "identifier": "ABC"}]
        self.requests = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def seen(self, method=None, path=None):
        with self.lock:
            return [r for r in self.requests
                    if (method is None or r["method"] == method) and (path is None or r["path"] == path)]

    def me(self, token):
        workspaces = {self.RUNNER: (self.workspace, "runner"), self.SESSION: (self.workspace, "session"),
                      self.OTHER: ("org_other", "runner"), self.OFF: (self.workspace, "runner")}
        if token not in workspaces:
            return 401, {"error": "Token de agente inválido.", "code": "invalid_agent_token"}
        if token == self.OFF:
            return 403, {"error": "O acesso de agentes está desativado neste workspace.", "code": "agents_disabled"}
        clerk, purpose = workspaces[token]
        return 200, {"data": {
            "agent": {"id": 7, "clerkId": "user_agent", "name": "Agent Marker"},
            "writing": {"language": self.language},
            "workspace": {"clerkId": clerk, "name": "Workspace Marker", "sigla": "WM", "appUrl": "https://unio.so"},
            "teams": self.teams,
            "token": {"id": 9, "name": "Runner", "prefix": token[:12], "purpose": purpose,
                      "expiresAt": "2027-01-01T00:00:00.000Z", "expiresInDays": 70},
        }}

    def route(self, request):
        """(status, body) for one request; the routes past /agents/me are added by the tests that need them."""
        if request["method"] == "GET" and request["path"] == "/v1/agents/me":
            return self.me(request["token"])
        return 404, {"error": "Não encontrado.", "code": "not_found"}


def _handler(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_any(self):
            url = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = raw.decode("utf-8", "replace")
            request = {"method": self.command, "path": url.path, "query": urllib.parse.parse_qs(url.query),
                       "body": body, "headers": dict(self.headers),
                       "token": (self.headers.get("Authorization") or "").replace("Bearer ", "", 1)}
            with fake.lock:
                fake.requests.append(request)
            status, payload = fake.route(request)
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = handle_any

    return Handler
