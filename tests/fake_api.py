"""A stand-in for the Unio API route the plugin's scripts call, for tests/run.sh.

GET /v1/agents/me answers by bearer token: the good tokens get an agent
whose skill block is read, on every request, from the file given on the
command line (a copy of tests/fixtures/skill_block.json, the contract the
backend tests too); the refused token gets 401 and the switched-off one 403.
Each request's X-Unio-Client header, or "-", is appended to the log file.

Usage: fake_api.py <port> <log> <skill block file> <refused> <off> <good>...
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT, LOG, SKILL_BLOCK, REFUSED, OFF = sys.argv[1:6]
GOOD = set(sys.argv[6:])

# Names an administrator typed: the scripts must never repeat them to Claude.
WORKSPACE = 'WorkspaceNameMarker "quoted"'
AGENT = "AgentNameMarker"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
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
            skill = json.load(block)["skill"]
        return self.answer(200, {"data": {
            "agent": {"id": 7, "clerkId": "user_agent", "name": AGENT, "avatarUrl": None,
                      "voice": {"tone": "direct", "notes": None}},
            "workspace": {"clerkId": "org_test", "name": WORKSPACE, "sigla": "WS",
                          "appUrl": "https://unio.so"},
            "teams": [{"id": 1, "name": "Team", "identifier": "ABC"}],
            "issuer": {"id": 3, "clerkId": "user_issuer", "name": "IssuerNameMarker"},
            "token": {"id": 9, "name": "Laptop", "prefix": token[:12],
                      "expiresAt": "2026-10-27T00:00:00.000Z", "expiresInDays": 5},
            "skill": skill,
        }})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(PORT)), Handler).serve_forever()
