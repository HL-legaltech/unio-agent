"""`unio resume`: the Claude Code session that worked on a Unio task, or one
session by its id, resumed in the folder it started in.

    unio resume DEV-380           the newest session bound to DEV-380
    unio resume DEV-380 --list    lists them and resumes nothing
    unio resume DEV-380 --print   prints the command instead of running it
    unio resume <session id>      that session; its first 8 characters do
                                  while no other session starts with them

The session hook keeps each session's bound task (with the tasks it worked on
before, and the follow-ups it filed) in the sessions folder as <session id>.json;
the transcript in ~/.claude/projects (or CLAUDE_CONFIG_DIR) says which folder
the session started in, which is where `claude --resume` must run. A key, a
task id or a Unio task link all work. A session id is looked up among the
transcripts themselves, so it works for sessions never bound to a task (the
agent's page shows it to its owner).

The help people read is bin/unio's; this prints Brazilian Portuguese, as all
runtime text does.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import system

SESSION_FILE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$")
KEY = re.compile(r"\b([A-Za-z0-9]{2,5}-\d+)\b")
SESSION_ID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?")
TRANSCRIPT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$")
WORKED = "worked"
FILED = "filed"


def projects():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def clean(text):
    """Text a person typed, safe to print on a terminal."""
    return re.sub(r"[\x00-\x1f\x7f]", "", text)[:80]


def tilde(path):
    if path is None:
        return "pasta desconhecida"
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def wanted(arg):
    """A task key (upper case) or id from what was typed."""
    found = re.search(r"/app/tasks/([^/?#]+)", arg)
    ref = found.group(1) if found else arg.strip()
    return ref.upper() if KEY.fullmatch(ref) else ref.lower()


def transcript(session_id):
    for path in projects().glob(f"*/{session_id}.jsonl"):
        return path
    return None


def started_in(path):
    """The folder the session started in: the first message's cwd."""
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    cwd = json.loads(line).get("cwd")
                except (ValueError, AttributeError):
                    continue
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return None


def sessions_named(arg):
    """The sessions whose id is, or starts with, what was typed (newest
    transcript per id); None when it does not look like a session id."""
    ref = arg.strip().lower()
    if not SESSION_ID.fullmatch(ref):
        return None
    found = {}
    for path in projects().glob(f"*/{ref}*.jsonl"):
        if TRANSCRIPT.match(path.name):
            at = path.stat().st_mtime
            if path.stem not in found or at > found[path.stem]["at"]:
                found[path.stem] = {"id": path.stem, "cwd": started_in(path), "at": at,
                                    "role": WORKED, "task": None}
    return sorted(found.values(), key=lambda s: -s["at"])


def sessions_for(ref):
    found = []
    state = system.state_dir()
    for entry in state.iterdir() if state.is_dir() else []:
        if not SESSION_FILE.match(entry.name):
            continue
        try:
            data = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        task = data.get("task") or {}
        follow_ups = data.get("follow_ups") or []
        # `earlier`: tasks the session finished before moving on to a follow-up.
        worked = [task] + (data.get("earlier") or [])
        if any(ref in (str(t.get("key", "")).upper(), t.get("id")) for t in worked):
            role = WORKED
        elif any(ref in (str(f.get("key", "")).upper(), f.get("id")) for f in follow_ups):
            role = FILED
        else:
            continue
        session_id = entry.name[:-len(".json")]
        path = transcript(session_id)
        if not path:
            continue  # the transcript is gone, so it cannot be resumed
        found.append({"id": session_id, "cwd": started_in(path), "at": path.stat().st_mtime,
                      "role": role, "task": task.get("key")})
    # The sessions that worked on it first, newest first within each.
    found.sort(key=lambda s: (s["role"] != WORKED, -s["at"]))
    return found


def shell_folder(path):
    """The folder as a shell word: quoted, with a leading ~/ left outside the quotes so it still expands."""
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~/" + shlex.quote(path[len(home) + 1:])
    return shlex.quote(path)


def command(session):
    if system.WINDOWS:
        return f"claude --resume {session['id']}, na pasta {session['cwd']}"
    return f"cd {shell_folder(session['cwd'])} && claude --resume {session['id']}"


def usage(message):
    print(message, file=sys.stderr)
    print("Rode unio help resume.", file=sys.stderr)
    return 2


def main(argv):
    args, flags = [], set()
    for a in argv:
        if a.startswith("-"):
            if a not in ("--list", "--print"):
                return usage(f"Opção desconhecida: {clean(a)}.")
            flags.add(a)
        else:
            args.append(a)
    if len(args) != 1:
        return usage("Diga qual sessão retomar: a chave ou o link de uma tarefa, ou o id da sessão.")
    named = sessions_named(args[0])
    if named and len(named) > 1:
        print(f"{clean(args[0])} é o começo de {len(named)} ids de sessão; digite mais caracteres:", file=sys.stderr)
        for s in named:
            print(f"  {s['id']}  {tilde(s['cwd'])}", file=sys.stderr)
        return 1
    ref = named[0]["id"] if named else wanted(args[0])
    found = named or sessions_for(ref)
    if not found:
        if named is not None:
            print(f"Nenhuma conversa em {tilde(str(projects()))} tem o id {clean(args[0])} (o Claude Code apaga"
                  " as conversas depois de 30 dias), e nenhuma sessão ligada a uma tarefa tem esse id.",
                  file=sys.stderr)
        else:
            print(f"Nenhuma sessão deste computador está ligada a {clean(args[0])}. Só aparecem as sessões"
                  " que o plugin do Unio acompanhou.", file=sys.stderr)
        return 1

    print(f"{clean(ref)}: {len(found)} {'sessões' if len(found) > 1 else 'sessão'}")
    for n, s in enumerate(found, 1):
        when = datetime.fromtimestamp(s["at"]).strftime("%Y-%m-%d %H:%M")
        note = "" if s["role"] == WORKED else f"  (criou esta tarefa a partir de {s['task']})"
        print(f"  {n}  {when}  {s['id']}  {tilde(s['cwd'])}{note}")
    if "--list" in flags:
        return 0

    pick = 1
    if len(found) > 1 and sys.stdin.isatty() and "--print" not in flags:
        try:
            answer = input("Retomar qual? [1] ").strip()
        except EOFError:
            answer = ""
        if answer:
            if not answer.isdigit() or not 1 <= int(answer) <= len(found):
                print("Esse número não está na lista.", file=sys.stderr)
                return 1
            pick = int(answer)
    session = found[pick - 1]
    if session["cwd"] is None:
        # Never another folder in its place: claude --resume finds a session only where it started.
        print(f"O registro da sessão {session['id']} não diz em que pasta ela começou. Rode claude --resume"
              f" {session['id']} na pasta do projeto em que ela trabalhou.", file=sys.stderr)
        return 1
    if not os.path.isdir(session["cwd"]):
        # A deleted worktree, often: the page offers this command for 30 days.
        print(f"A sessão começou em {tilde(session['cwd'])}, que não existe mais, e o claude --resume precisa"
              f" rodar lá. Recrie a pasta e rode: {command(session)}", file=sys.stderr)
        return 1
    if "--print" in flags:
        print(command(session))
        return 0
    print(f"Retomando: {command(session)}")
    claude = shutil.which("claude")
    if not claude:
        print("O comando claude não está no PATH. Instale o Claude Code e rode este comando de novo.",
              file=sys.stderr)
        return 1
    sys.stdout.flush()  # exec drops what a piped stdout still buffers
    if system.WINDOWS:
        # Windows has no exec: os.execv would start claude and end this
        # process, handing the console back to the shell under it.
        return subprocess.call([claude, "--resume", session["id"]], cwd=session["cwd"])
    os.chdir(session["cwd"])
    os.execv(claude, [claude, "--resume", session["id"]])
    return 1
