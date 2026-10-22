"""A stand-in for the Unio API route the plugin's scripts call, for tests/run.sh.

GET /v1/agents/me answers by bearer token: the refused token gets 401, the
switched-off one 403, and the good tokens an agent in workspace org_test,
or in the workspace named after "=" (<token>=org_X). Its skill block, and
its token's days left when the file sets "expiresInDays" (5 otherwise),
are read on every request from the file given on the command line: a copy
of tests/fixtures/skill_block.json, the contract the backend tests too.
Each request's X-Unio-Client header, or "-", is appended to the log file.
Bodies are compact JSON, as the backend sends them, so the scripts' sed
patterns are tested against no spaces around colons and commas.

The browser approval's two routes read their script from the folder in
FAKE_CONNECT_DIR: "start" holds a status to answer start with instead of 201
(404 before the routes exist), "ttl" the seconds a request lasts, and
"polls" one answer per line, each consumed by a poll: pending, slow_down,
denied, gone, or "approved <good token>". With no line left, a poll is
pending. Each request's path and body, and each poll's time, go to "log".

Usage: fake_api.py <port> <log> <skill block file> <refused> <off> <good>[=<org>]...
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT, LOG, SKILL_BLOCK, REFUSED, OFF = sys.argv[1:6]
GOOD = dict((arg.split("=", 1) + ["org_test"])[:2] for arg in sys.argv[6:])

CONNECT = os.environ.get("FAKE_CONNECT_DIR", "")
# A device code in the real shape (43 base64url characters), so the scripts
# accept it and the tests can look for it.
DEVICE_CODE = "FakeDeviceCodeNotASecret-" + "0" * 18

# Names an administrator typed: the scripts must never repeat them to Claude.
WORKSPACE = 'WorkspaceNameMarker "quoted"'
AGENT = "AgentNameMarker"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, body):
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with open(LOG, "a") as log:
            log.write((self.headers.get("X-Unio-Client") or "-") + "\n")
        if self.path != "/v1/agents/me":
            return self.answer(404, {"error": "Não encontrado."})
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if token == OFF:
            return self.answer(403, {"error": "Agentes desligados.", "code": "agents_disabled"})
        if token not in GOOD:
            return self.answer(401, {"error": "Token de agente inválido.", "code": "invalid_agent_token"})
        with open(SKILL_BLOCK) as block:
            served = json.load(block)
        return self.answer(200, {"data": {
            "agent": {"id": 7, "clerkId": "user_agent", "name": AGENT, "avatarUrl": None,
                      "voice": {"tone": "direct", "notes": None}},
            "workspace": {"clerkId": GOOD[token], "name": WORKSPACE, "sigla": "WS",
                          "appUrl": "https://unio.so"},
            "teams": [{"id": 1, "name": "Team", "identifier": "ABC"}],
            "issuer": {"id": 3, "clerkId": "user_issuer", "name": "IssuerNameMarker"},
            "token": {"id": 9, "name": "Laptop", "prefix": token[:12],
                      "expiresAt": "2026-10-27T00:00:00.000Z",
                      "expiresInDays": served.get("expiresInDays", 5)},
            "skill": served["skill"],
        }})

    def connect_file(self, name, default=""):
        try:
            with open(os.path.join(CONNECT, name)) as f:
                return f.read().strip()
        except OSError:
            return default

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        with open(os.path.join(CONNECT, "log"), "a") as log:
            log.write("%s %.3f %s\n" % (self.path, time.time(), raw))
        try:
            body = json.loads(raw)
        except ValueError:
            return self.answer(400, {"error": "JSON inválido."})
        if self.path == "/v1/agents/connect/start":
            status = self.connect_file("start")
            if status:
                return self.answer(int(status), {"error": "Não encontrado."})
            if body.get("client") not in ("claude-code", "codex") or not body.get("host"):
                return self.answer(422, {"error": "Pedido inválido."})
            return self.answer(201, {"data": {
                "deviceCode": DEVICE_CODE, "userCode": "BCDF-GHJK",
                "verifyUrl": "https://unio.so/app/connect?code=BCDF-GHJK",
                "interval": 1, "expiresIn": int(self.connect_file("ttl", "600")),
            }})
        if self.path == "/v1/agents/connect/poll":
            if body.get("deviceCode") != DEVICE_CODE:
                return self.answer(400, {"error": "Pedido inválido.", "code": "invalid_request"})
            lines = self.connect_file("polls").splitlines()
            step = lines[0].split() if lines else ["pending"]
            with open(os.path.join(CONNECT, "polls"), "w") as f:
                f.write("\n".join(lines[1:]))
            if step[0] == "slow_down":
                return self.answer(429, {"error": "Devagar.", "code": "slow_down"})
            if step[0] == "denied":
                return self.answer(403, {"error": "Recusado.", "code": "access_denied"})
            if step[0] == "gone":
                return self.answer(410, {"error": "Venceu.", "code": "expired_token"})
            if step[0] == "approved":
                token = step[1]
                return self.answer(200, {"data": {
                    "token": token,
                    "workspace": {"clerkId": GOOD[token], "name": WORKSPACE},
                    "agent": {"name": AGENT},
                    "connection": {"id": "c1", "name": "host", "expiresAt": "2026-11-21T00:00:00.000Z",
                                   "expiresInDays": 30},
                }})
            return self.answer(202, {"data": {"status": "authorization_pending"}})
        return self.answer(404, {"error": "Não encontrado."})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(PORT)), Handler).serve_forever()
