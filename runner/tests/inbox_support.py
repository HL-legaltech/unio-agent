"""What the runner's tests share: unio-inbox loaded as a module, a throwaway
home for each test, a fake Unio API on 127.0.0.1, and a fake claude.

Stdlib only, like the runner, so the tests run on any Python 3.9 or later
on Linux, macOS and Windows: python -m unittest discover -s runner/tests
"""
import importlib.machinery
import importlib.util
import json
import os
import shlex
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
# No test resolves a real name: the web check's lookup answers "does not resolve".
REAL_RESOLVE_HOST = inbox.resolve_host
inbox.resolve_host = lambda host: ()


def non_utf8_locale(env):
    """`env` for a program whose pipes Python would not write in UTF-8. On
    Windows they get the ANSI code page anyway; elsewhere the C locale makes
    them ASCII, once neither UTF-8 mode nor locale coercion undoes it."""
    env = {k: v for k, v in env.items()
           if k not in ("PYTHONIOENCODING", "LANG", "LANGUAGE") and not k.startswith("LC_")}
    return dict(env, LC_ALL="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")


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
        # What GET /agents/inbox lists; a completed item leaves it.
        self.items = []
        self.task_files_refused = False
        # An HTTP status every progress post that marks a research run gets, or None.
        self.research_mark_status = None
        # An HTTP status every runner create_comment over MCP gets, or None.
        self.mcp_comment_status = None
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
        """(status, body) for one request: the routes an inbox run calls."""
        method, path = request["method"], request["path"]
        if method == "GET" and path == "/v1/agents/me":
            return self.me(request["token"])
        if request["token"] not in (self.RUNNER, self.SESSION, self.OTHER):
            return 401, {"error": "Token de agente inválido.", "code": "invalid_agent_token"}
        body = request["body"] if isinstance(request["body"], dict) else {}
        if method == "GET" and path == "/v1/agents/inbox":
            with self.lock:
                return 200, {"data": list(self.items)}
        if method == "POST" and path == "/v1/agents/inbox/claim":
            return 200, {"data": {"key": body.get("key"), "claimId": "claim-0001-fake", "attempts": 0}}
        if method == "POST" and path in ("/v1/agents/inbox/complete", "/v1/agents/inbox/fail",
                                         "/v1/agents/inbox/release"):
            if path.endswith("/complete"):
                with self.lock:
                    self.items = [i for i in self.items if i.get("key") != body.get("key")]
            return 200, {"data": {"key": body.get("key"), "attempts": 1}}
        if method == "POST" and path == "/v1/agents/inbox/progress":
            if self.research_mark_status and body.get("mode") == "research":
                return self.research_mark_status, {"error": "Erro.", "code": "server_error"}
            return 200, {"data": {}}
        if method in ("PUT", "DELETE") and path == "/v1/agents/reactions":
            return 200, {"data": {}}
        if method == "PUT" and path.startswith("/v1/agents/presence/"):
            return 200, {"data": {}}
        if method == "POST" and path.startswith("/v1/tasks/") and path.endswith("/comments"):
            return 201, {"data": {"id": "c-runner-reply"}}
        if method == "POST" and path == "/v1/agents/inbox/files":
            if body.get("target") == "task" and self.task_files_refused:
                return 403, {"error": "Sem permissão.", "code": "agent_scope_author"}
            return 201, {"data": {"id": "a-0001", "name": body.get("name")}}
        if method == "PUT" and path.startswith("/v1/comments/"):
            return 200, {"data": {"id": path.rsplit("/", 1)[1]}}
        if method == "POST" and path == "/v1/mcp":
            name = ((body.get("params") or {}).get("name"))
            if name == "create_comment" and self.mcp_comment_status:
                code = "agent_research_comment_cap" if self.mcp_comment_status == 409 else "unavailable"
                return self.mcp_comment_status, {"error": "Recusado.", "code": code}
            # create_comment's answer (write_tools.ts): the new comment's id, its time and its task.
            answer = {"id": "c-session-reply", "createdAt": "2026-10-27T12:00:00.000Z",
                      "task": {"key": "ABC-1"}} if name == "create_comment" else {}
            return 200, {"jsonrpc": "2.0", "id": body.get("id"),
                         "result": {"content": [{"type": "text", "text": json.dumps(answer)}]}}
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


FAKE_CLAUDE = r'''
"""A stand-in for Claude Code, for the runner's tests. `auth status --json`
says it is signed in. A session (-p) reads its prompt on stdin, writes what
it was given as one JSON line to $FAKE_CLAUDE_LOG, makes the folder Claude
Code keeps for its working folder, and prints a canned stream-json run:
init (its --tools and the unio tools the server registers), get_task, then a
create_comment it really sends to the MCP server in its --mcp-config, then a
result with the outcome line. FAKE_CLAUDE_MODE=too_large ends at the turn cap
instead, having written nothing; escalate makes a reply run hand the item to
research. A research run (the researcher protocol) searches, reads a page,
posts its checkpoint with the prompt's marker and edits it without the marker;
research_cap stops at the turn cap after the checkpoint, research_silent
before it; research_file also writes a CSV into the prompt's files folder and
names it."""
import json
import os
import re
import sys
import urllib.request

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    sys.exit(0)
if args == ["--version"]:
    print("2.1.300 (Claude Code)")
    sys.exit(0)


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


prompt = sys.stdin.buffer.read().decode("utf-8")
protocol_file = value("--append-system-prompt-file")
with open(protocol_file, encoding="utf-8") as f:
    protocol = f.read()
with open(value("--mcp-config"), encoding="utf-8") as f:
    unio = json.load(f)["mcpServers"]["unio"]
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "cwd": os.getcwd(), "prompt": prompt, "protocol": protocol,
                        "headers": sorted(unio["headers"])}) + "\n")
slug = re.sub(r"[^a-zA-Z0-9]", "-", os.getcwd())
os.makedirs(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", slug), exist_ok=True)


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def use(n, name, arguments, ok, text, structured=None):
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"tu{n}", "name": name, "input": arguments}]}})
    answer = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"tu{n}", "is_error": not ok,
         "content": [{"type": "text", "text": text}]}]}}
    if structured is not None:
        answer["tool_use_result"] = structured
    emit(answer)


def result(text, subtype="success", turns=3):
    emit({"type": "result", "subtype": subtype, "is_error": subtype != "success", "num_turns": turns,
          "total_cost_usd": 0.0123, "result": text,
          "modelUsage": {"claude-test": {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 1,
                                         "cacheCreationInputTokens": 2, "costUSD": 0.0123}}})


def post_comment(body):
    """create_comment through the MCP server in --mcp-config, as Claude Code would."""
    arguments = {"task": task.group(1), "body": body, "reply_to": comment}
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "create_comment", "arguments": arguments}}
    request = urllib.request.Request(unio["url"], data=json.dumps(call).encode(), method="POST",
                                     headers={**unio["headers"], "Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(request, timeout=20) as response:
        answer = json.loads(response.read())
    text = answer["result"]["content"][0]["text"]
    use(2, "mcp__unio__create_comment", arguments, not answer["result"].get("isError"), text)
    return json.loads(text).get("id")


mode = os.environ.get("FAKE_CLAUDE_MODE") or ""
builtin = [t for t in (value("--tools") or "").split(",") if t]
# Claude Code leaves a tool its deny rules name out of the init.
denied = set((value("--disallowedTools") or "").split(","))
emit({"type": "system", "subtype": "init", "claude_code_version": "2.1.300",
      "tools": builtin + [t for t in ("mcp__unio__get_task", "mcp__unio__list_comments", "mcp__unio__create_comment",
                                      "mcp__unio__update_comment", "mcp__unio__add_reaction",
                                      "mcp__unio__update_task") if t not in denied],
      "mcp_servers": [{"name": "unio", "status": "connected"}]})
task = re.search(r"^Task: (\S+) \(id ([^)]+)\)", prompt, re.M)
use(1, "mcp__unio__get_task", {"task": task.group(1)}, True, json.dumps({"id": task.group(2)}))
comment = (re.search(r"^Comment id: (\S+)", prompt, re.M) or [None, None])[1]
content = "Resposta de teste." if "Writing language: Brazilian Portuguese." in prompt else "Test answer."
if "# Unio Bot: researching" in protocol:
    marker = re.search(r"^Checkpoint marker: .*?edit: (.+)$", prompt, re.M).group(1)
    emit({"type": "rate_limit_event", "rate_limit_info": {"rateLimitType": "seven_day", "utilization": 0.5,
                                                         "resetsAt": 1793188800}})
    if "WebSearch" in builtin:
        use(3, "WebSearch", {"query": "aliquota iss software", "mode": "standard"}, True, "Links: []")
    if "WebFetch" in builtin:
        use(4, "WebFetch", {"url": "https://www.example.org/lei", "prompt": "rates"}, True, "rates",
            {"bytes": 5, "code": 200, "codeText": "OK", "result": "rates", "url": "https://www.example.org/lei"})
    if mode == "research_silent":
        result("", "error_max_turns", 60)
        sys.exit(1)
    if mode == "research_file":
        folder = re.search(r"^Files: write only into (.+), and only when", prompt, re.M).group(1)
        rows = ["municipio,aliquota,fonte"] + [f"Cidade {n},{n % 5}%,https://www.example.org/{n}" for n in range(80)]
        with open(os.path.join(folder, "dados-teste-2026.csv"), "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        use(5, "Write", {"file_path": os.path.join(folder, "dados-teste-2026.csv")}, True, "written")
        content += "\n\nAnexei **dados-teste-2026.csv**: as 80 linhas com município e alíquota, para filtrar."
    answer_id = post_comment(content + "\n\n" + marker)
    if mode == "research_cap":
        result("", "error_max_turns", 60)
        sys.exit(1)
    use(6, "mcp__unio__update_comment", {"comment_id": answer_id,
                                       "body": content + "\n\nFinal ([lei](https://www.example.org/lei))."}, True,
        json.dumps({"id": answer_id}))
    result(content + "\n{\"outcome\":\"replied\",\"note\":\"researched\"}", turns=12)
    sys.exit(0)
if mode == "too_large":
    result("", "error_max_turns", 30)
    sys.exit(1)
if mode == "escalate":
    result("Needs the web.\n{\"outcome\":\"research\",\"question\":\"ISS rates for software\"}")
    sys.exit(0)
post_comment(content)
result(content + "\n{\"outcome\":\"replied\",\"note\":\"answered the question\"}")
'''


def write_fake_claude(folder, windows=None):
    """The fake claude in `folder`, as a program PATH finds: a sh launcher
    named claude on POSIX, claude.cmd on Windows, where a Python script
    cannot run by its name. Returns the launcher's path."""
    windows = os.name == "nt" if windows is None else windows
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "fake_claude.py"
    script.write_text(FAKE_CLAUDE.lstrip(), encoding="utf-8")
    if windows:
        launcher = folder / "claude.cmd"
        launcher.write_bytes(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n'.encode("utf-8"))
    else:
        launcher = folder / "claude"
        launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n",
                            encoding="utf-8")
        launcher.chmod(0o755)
    return launcher
