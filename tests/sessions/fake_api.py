"""The two routes coding sessions report through, in memory, on a loopback
socket: PUT /v1/agents/presence/:id and PUT /v1/agents/sessions/:id, with the
backend's validation and merge rules (docs/agent-status.md in the backend).
A clock offset lets a test judge times as the server would."""
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

TOKEN = "unio_" + ("SessionsFakeTokenNotASecret" * 2)[:46]
PRESENCE_SPACING = timedelta(seconds=10)
REASONS = {"signed_out", "usage_limit", "model_unavailable", "unio_unavailable", "unexpected_tools", "budget",
           "repo_unavailable", "other"}
STEPS = {"reading_task", "reading_thread", "reading_code", "searching_tasks", "writing_reply", "updating_task",
         "filing_follow_up", "reacting", "searching_projects", "reading_project", "creating_project",
         "updating_project", "reviewing_project", "thinking", "searching_code", "editing_code", "running_command",
         "running_tests", "running_checks", "using_git", "researching", "delegating", "waiting_for_person"}
PRESENCE_KINDS = {"inbox", "session"}
PRESENCE_STATES = {"idle", "working", "problem", "ended"}
# agent_access.ts: the body fields each route accepts; anything else is 422 agent_field_forbidden.
BODY_FIELDS = {
    "presence": {"kind", "host", "client", "state", "taskId", "itemKey", "step", "message", "reason"},
    "sessions": {"claudeSessionId", "host", "taskId", "startedAt", "lastActivityAt", "endedAt", "countedSince",
                 "model", "contextTokens", "contextLimit", "activeAgents", "title", "hours"},
}
LIMITS = {"host": 64, "client": 64, "step": 80, "message": 300}
HOUR_FIELDS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
MODEL = re.compile(r"^claude-[a-z0-9.-]{1,48}(\[1m\])?$")
INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def parse_iso(value):
    """An ISO 8601 time with an offset; Python 3.9's fromisoformat reads no Z
    and only 3 or 6 fraction digits."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})",
                     value) if isinstance(value, str) else None
    if not m:
        return None
    zone, frac = m.group(8), m.group(7) or ""
    tz = timezone.utc if zone == "Z" else timezone(
        (1 if zone[0] == "+" else -1) * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])))
    try:
        return datetime(*(int(x) for x in m.groups()[:6]), int((frac + "000000")[:6]), tzinfo=tz)
    except ValueError:
        return None


def integer(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def utf16_length(text):
    """String length as JavaScript, and so Vine's maxLength, counts it."""
    return len(text.encode("utf-16-le")) // 2


class FakeApi:
    def __init__(self):
        self.tasks = set()  # task ids in the agent's teams
        self.requests = []
        self.presences = {}
        self.presence_writes = []
        self.sessions = {}
        self.session_writes = []
        self.sessions_status = None  # an answer every session report gets instead, e.g. 403 from an older server
        self.sessions_refused_fields = set()  # fields the route refuses as agent_field_forbidden
        self.hang = threading.Event()
        self.presence_delays = []  # seconds each next presence write waits before it is handled
        self.offset = timedelta(0)
        self.lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_PUT(self):
                api.dispatch(self, "PUT")

            def do_GET(self):
                api.dispatch(self, "GET")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *a: None  # a hung request's client is gone by then
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.hang.clear()
        self.server.shutdown()
        self.server.server_close()

    def now(self):
        return datetime.now(timezone.utc) + self.offset

    def task(self):
        task_id = str(uuid.uuid4())
        self.tasks.add(task_id)
        return task_id

    def presence_calls(self):
        return [r for r in self.requests if r["path"].startswith("/v1/agents/presence/")]

    def session_calls(self):
        return [r for r in self.requests if r["path"].startswith("/v1/agents/sessions/")]

    def dispatch(self, h, method):
        n = int(h.headers.get("Content-Length") or 0)
        try:
            body = json.loads(h.rfile.read(n)) if n else None
        except ValueError:
            body = None
        url = urlparse(h.path)
        while self.hang.is_set():  # an API that accepts and never answers
            time.sleep(0.05)
        if url.path.startswith("/v1/agents/presence/") and self.presence_delays:
            time.sleep(self.presence_delays.pop(0))
        with self.lock:
            self.requests.append({"method": method, "path": url.path, "body": body,
                                  "authorization": h.headers.get("Authorization"),
                                  "at": datetime.now(timezone.utc)})
            status, payload = self.route(h, method, url.path, body)
        raw = json.dumps(payload).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    def route(self, h, method, path, body):
        if h.headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"error": "Token de agente inválido.", "code": "invalid_agent_token"}
        body = body or {}
        kind = "presence" if path.startswith("/v1/agents/presence/") else \
            "sessions" if path.startswith("/v1/agents/sessions/") else None
        if method != "PUT" or kind is None:
            return 404, {"error": "no route"}
        if set(body) - BODY_FIELDS[kind]:
            field = sorted(set(body) - BODY_FIELDS[kind])[0]
            return 422, {"error": f'Agentes não podem enviar o campo "{field}".', "code": "agent_field_forbidden",
                         "field": field}
        for field, limit in LIMITS.items():
            if isinstance(body.get(field), str) and len(body[field]) > limit:
                return 422, {"error": f"{field} longo demais.", "field": field}
        if kind == "sessions":
            return self.session(path.rsplit("/", 1)[1], body)
        return self.presence(path.rsplit("/", 1)[1], body)

    def presence(self, presence_id, body):
        """Upsert (agent_presence_service.ts): a change of state, task or item is stored at
        once; any other write within 10 s of the last stored one is dropped, still 200."""
        if not is_uuid(presence_id):
            return 422, {"error": "id inválido.", "field": "id"}
        if body.get("kind") not in PRESENCE_KINDS or body.get("state") not in PRESENCE_STATES:
            return 422, {"error": "Presença inválida."}
        if not str(body.get("host") or "").strip() or not str(body.get("client") or "").strip():
            return 422, {"error": "host e client são obrigatórios."}
        if body.get("reason") is not None and body["reason"] not in REASONS:
            return 422, {"error": "Motivo inválido.", "field": "reason"}
        if body.get("step") is not None and body["step"] not in STEPS:
            return 422, {"error": "Etapa inválida.", "field": "step"}
        if body.get("taskId") is not None and body["taskId"] not in self.tasks:
            return 422, {"error": "Tarefa fora dos times do agente.", "field": "taskId"}
        now = self.now()
        current = self.presences.get(presence_id)
        stored = (current is None or any(body.get(k) != current.get(k) for k in ("state", "taskId", "itemKey"))
                  or now - current["storedAt"] >= PRESENCE_SPACING)
        self.presence_writes.append({"id": presence_id, "body": body, "stored": stored, "at": now})
        if stored:
            self.presences[presence_id] = {**body, "storedAt": now, "lastSeenAt": now,
                                           "endedAt": now if body["state"] == "ended" else None}
        return 200, {"data": {"id": presence_id, "state": self.presences[presence_id]["state"]}}

    def session(self, session_id, body):
        """PUT /v1/agents/sessions/:id: the upsert and its merge rules."""
        if self.sessions_status is not None:
            return self.sessions_status, {"error": "Recusado.", "code": "agent_route_forbidden"}
        refused = sorted(set(body) & self.sessions_refused_fields)
        if refused:
            return 422, {"error": f'Agentes não podem enviar o campo "{refused[0]}".',
                         "code": "agent_field_forbidden", "field": refused[0]}
        if body.get("taskId") is not None and body["taskId"] not in self.tasks:
            return 422, {"error": "A tarefa informada não pertence às equipes deste agente.",
                         "code": "agent_task_forbidden", "field": "taskId"}
        now = self.now()
        refusal = check_session(session_id, body, now)
        self.session_writes.append({"id": session_id, "body": body, "at": now,
                                    "status": refusal[0] if refusal else 200})
        if refusal:
            return refusal
        row = self.sessions.get(session_id)
        t = {k: parse_iso(body.get(k)) for k in ("startedAt", "lastActivityAt", "endedAt", "countedSince")}
        if row is None:
            row = self.sessions[session_id] = {"hours": {}, "startedAt": t["startedAt"],
                                               "lastActivityAt": t["lastActivityAt"], "countedSince": None,
                                               "model": None, "title": None}
        row["startedAt"] = min(row["startedAt"], t["startedAt"])
        row["lastActivityAt"] = max(row["lastActivityAt"], t["lastActivityAt"])
        row["endedAt"] = t["endedAt"]
        row["countedSince"] = row["countedSince"] or t["countedSince"]
        for key in ("host", "taskId", "contextTokens", "contextLimit", "activeAgents"):
            row[key] = body.get(key)
        if body.get("model") is not None:
            row["model"] = body["model"]
        if "title" in body:
            row["title"] = body["title"]
        row["reportedAt"] = now
        for h in body.get("hours") or []:
            old = row["hours"].get(h["hour"], {})
            row["hours"][h["hour"]] = {k: max(old.get(k, 0), h[k]) for k in (*HOUR_FIELDS, "activeMs")}
        return 200, {"data": {"id": session_id, "stored": True}}

    def session_totals(self, session_id):
        """SUM of a session's buckets, as the reads compute it."""
        row = self.sessions.get(session_id) or {"hours": {}}
        return {k: sum(h[k] for h in row["hours"].values()) for k in (*HOUR_FIELDS, "activeMs")}


def session_refusal(field, code):
    return 422, {"error": f"{field} inválido.", "code": code, "field": field}


def vine_messages(body):
    """The validator's failures, as the backend answers them: a list of
    {message, rule, field} under `messages`, no code and no top-level field."""
    found = []

    def fail(field, rule):
        found.append({"message": f"The {field} field is invalid", "rule": rule, "field": field})

    def text(field, required, check, rule):
        value = body.get(field)
        if value is None:
            if required:
                fail(field, "required")
        elif not isinstance(value, str) or not check(value):
            fail(field, rule)

    text("claudeSessionId", True, is_uuid, "uuid")
    text("host", True, lambda v: 1 <= len(v.strip()) <= 64, "maxLength")
    text("taskId", False, is_uuid, "uuid")
    for field, required in (("startedAt", True), ("lastActivityAt", True), ("endedAt", False), ("countedSince", False)):
        text(field, required, INSTANT.match, "regex")
    text("model", False, MODEL.match, "regex")
    text("title", False, lambda v: utf16_length(v) <= 300, "maxLength")
    for field, low, high in (("contextTokens", 0, 10 ** 8), ("contextLimit", 1, 10 ** 8)):
        if body.get(field) is not None and not integer(body[field], low, high):
            fail(field, "range")
    if "activeAgents" in body and not integer(body["activeAgents"], 0, 1000):
        fail("activeAgents", "range")
    hours = body.get("hours")
    if hours is not None:
        if not isinstance(hours, list) or len(hours) > 500:
            fail("hours", "maxLength")
        else:
            for n, h in enumerate(hours):
                if not isinstance(h, dict) or set(h) != {"hour", "activeMs", *HOUR_FIELDS}:
                    fail(f"hours.{n}", "object")
                    continue
                if not isinstance(h["hour"], str) or not INSTANT.match(h["hour"]):
                    fail(f"hours.{n}.hour", "regex")
                for k in HOUR_FIELDS:
                    if not integer(h[k], 0, 10 ** 10):
                        fail(f"hours.{n}.{k}", "range")
                if not integer(h["activeMs"], 0, 3_600_000):
                    fail(f"hours.{n}.activeMs", "range")
    return found


def check_session(session_id, body, now):
    """The route's validation in the controller's order: the id, the
    validator, the id's match, then how the times and hours relate. None when
    the body passes."""
    if not is_uuid(session_id):
        return session_refusal("id", "agent_session_id_invalid")
    messages = vine_messages(body)
    if messages:
        return 422, {"error": "Validation failed", "messages": messages}
    if session_id.lower() != str(uuid.uuid5(uuid.NAMESPACE_URL, "unio-session:" + body["claudeSessionId"].lower())):
        return session_refusal("id", "agent_session_id_mismatch")
    times = {}
    for field in ("startedAt", "lastActivityAt", "endedAt", "countedSince"):
        if body.get(field) is None:
            continue
        t = parse_iso(body[field])
        if t is None or t > now + timedelta(minutes=5):
            return session_refusal(field, "agent_session_time_invalid")
        times[field] = t
    if times["lastActivityAt"] < now - timedelta(days=90):
        return session_refusal("lastActivityAt", "agent_session_time_invalid")
    if times["startedAt"] > times["lastActivityAt"]:
        return session_refusal("startedAt", "agent_session_time_invalid")
    low = times["startedAt"].replace(minute=0, second=0, microsecond=0)
    seen = set()
    for h in body.get("hours") or []:
        t = parse_iso(h["hour"])
        if (t is None or t.minute or t.second or t.microsecond or t < low or t > times["lastActivityAt"]
                or t < now - timedelta(days=90) or t in seen):
            return session_refusal("hours", "agent_session_hours_invalid")
        seen.add(t)
    return None
