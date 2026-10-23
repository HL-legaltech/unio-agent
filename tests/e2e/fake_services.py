"""Fake Anthropic and Unio APIs on one loopback port, for tests/e2e/claude_session.py.

A real Claude Code session talks to both: the Messages API through
ANTHROPIC_BASE_URL, and Unio's MCP server through the URL the test copy of
the plugin's .mcp.json names. Nothing leaves the machine and no real
credential is needed.

- POST /v1/messages answers "ok" (as a stream when asked) and logs the text
  Claude Code sent, so a test can find what a hook or a command put there.
- POST /v1/messages/count_tokens answers a fixed count.
- POST /v1/mcp is a minimal Streamable HTTP MCP server with no tools, and
  logs the bearer token and X-Unio-Client of each request.
- GET /v1/agents/me answers the plugin's scripts by token, like
  tests/fake_api.py, with the workspace given on the command line.
- POST /control/expect-mcp makes the next /v1/messages wait (up to 30 s)
  for an MCP request, so a session cannot end before Claude Code connects
  to Unio: in print mode it does not wait for the connection itself. It
  first waits out the connections the previous session left open, so none
  of their requests counts as this session's.

Every request becomes one JSON line in the log file.

Usage: fake_services.py <port file> <log file> <token>=<org>...
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT_FILE, LOG = sys.argv[1:3]
TOKENS = dict(arg.split("=", 1) for arg in sys.argv[3:])
LOCK = threading.Lock()
MCP_SEEN = threading.Event()
EXPECT_MCP = threading.Event()
OPEN = threading.Condition()
open_connections = 0


def log(entry):
    with LOCK, open(LOG, "a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")


def message_text(body):
    """Every text block Claude Code sent: system prompt and messages."""
    parts = []

    def walk(value):
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            if value.get("type") in (None, "text") and isinstance(value.get("text"), str):
                parts.append(value["text"])
            elif "content" in value:
                walk(value["content"])

    walk(body.get("system", []))
    walk(body.get("messages", []))
    return "\n".join(parts)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def send_json(self, status, body, headers=()):
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def send_empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def bearer(self):
        return (self.headers.get("Authorization") or "").replace("Bearer ", "", 1)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        token = self.bearer()
        log({"method": "GET", "path": path, "token": token,
             "client": self.headers.get("X-Unio-Client")})
        if path == "/v1/agents/me":
            if token not in TOKENS:
                return self.send_json(401, {"error": "Token de agente inválido."})
            return self.send_json(200, {"data": {
                "workspace": {"clerkId": TOKENS[token], "name": "Fake"},
                "token": {"expiresInDays": 300},
                "skill": {"status": "current", "current": "1.0.0", "minimum": "1.0.0"},
            }})
        if path == "/v1/mcp":
            return self.send_empty(405)
        return self.send_json(404, {"error": {"type": "not_found_error", "message": path}})

    def do_DELETE(self):
        self.send_empty(200)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self.read_body()
        if path == "/control/expect-mcp":
            # A request of the previous session still in a handler could set
            # MCP_SEEN after the clear below; only this connection may be open.
            with OPEN:
                OPEN.wait_for(lambda: open_connections <= 1, timeout=10)
            MCP_SEEN.clear()
            EXPECT_MCP.set()
            return self.send_empty(204)
        if path == "/v1/mcp":
            MCP_SEEN.set()
            return self.mcp(body)
        if path == "/v1/messages/count_tokens":
            return self.send_json(200, {"input_tokens": 10})
        if path == "/v1/messages":
            if EXPECT_MCP.is_set():
                MCP_SEEN.wait(30)
                EXPECT_MCP.clear()
            log({"method": "POST", "path": path, "stream": bool(body.get("stream")),
                 "text": message_text(body)})
            return self.message(body)
        log({"method": "POST", "path": path})
        return self.send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": path}})

    def mcp(self, body):
        log({"method": "POST", "path": "/v1/mcp", "token": self.bearer(),
             "client": self.headers.get("X-Unio-Client"), "rpc": body.get("method")})
        if self.bearer() not in TOKENS:
            return self.send_json(401, {"error": "Token de agente inválido."})
        if "id" not in body:
            return self.send_empty(202)
        method = body.get("method")
        if method == "initialize":
            version = (body.get("params") or {}).get("protocolVersion", "2025-06-18")
            result = {"protocolVersion": version, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake-unio", "version": "0.0.0"}}
        elif method == "tools/list":
            result = {"tools": []}
        elif method in ("prompts/list", "resources/list", "resources/templates/list"):
            key = method.split("/")[0] if method != "resources/templates/list" else "resourceTemplates"
            result = {key: []}
        else:
            return self.send_json(200, {"jsonrpc": "2.0", "id": body["id"],
                                        "error": {"code": -32601, "message": "Method not found"}})
        return self.send_json(200, {"jsonrpc": "2.0", "id": body["id"], "result": result})

    def message(self, body):
        model = body.get("model", "claude-fake")
        usage = {"input_tokens": 10, "output_tokens": 1,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if not body.get("stream"):
            return self.send_json(200, {
                "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn", "stop_sequence": None, "usage": usage,
            })
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "ok"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 1}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        data = "".join(
            "event: %s\ndata: %s\n\n" % (name, json.dumps(event, separators=(",", ":")))
            for name, event in events
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    """Counts the connections still being handled."""

    def process_request(self, request, client_address):
        global open_connections
        with OPEN:
            open_connections += 1
        super().process_request(request, client_address)

    def shutdown_request(self, request):
        global open_connections
        super().shutdown_request(request)
        with OPEN:
            open_connections -= 1
            OPEN.notify_all()


def main():
    server = Server(("127.0.0.1", 0), Handler)
    with open(PORT_FILE + ".tmp", "w", encoding="utf-8") as out:
        out.write(str(server.server_address[1]))
    # Renamed into place, so the driver never reads half a number.
    os.replace(PORT_FILE + ".tmp", PORT_FILE)
    server.serve_forever()


if __name__ == "__main__":
    main()
