"""Stands in for an interactive Claude Code that runs the session hook.

    fake_claude.py <session> [event...]

It runs the hook for each event named, then for each JSON line on stdin (more
fields for that event), prints how long each took, and lives until it is
killed. Like Claude Code 2.1.29x it gives every hook CLAUDE_PID (its own
pid), CLAUDE_CODE_CHILD_SESSION=1 and its own session id, whether or not it
is itself a child session.

FAKE_CLAUDE_HOOK is the hook's argv as JSON: on Linux and macOS the plugin's
command, `sh session-status.sh` under a shell as Claude Code runs it; on
Windows sessions.py itself. FAKE_CLAUDE_VIA_PROGRAM puts another program
between it and the hook. Copied to a file named `claude`, Linux names the
process claude, as it names the real one."""
import json
import os
import subprocess
import sys
import time


def main(session, *events):
    hook_env = {**os.environ, "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_PID": str(os.getpid()),
                "CLAUDE_CODE_SESSION_ID": os.environ.get("FAKE_CLAUDE_OWN_SESSION") or session}
    command = json.loads(os.environ["FAKE_CLAUDE_HOOK"])
    if os.environ.get("FAKE_CLAUDE_VIA_PROGRAM"):
        command = [sys.executable, "-c", "import subprocess, sys; subprocess.run(sys.argv[1:])", *command]

    def run(event, **extra):
        data = {"session_id": session, "hook_event_name": event, "cwd": os.getcwd(),
                "transcript_path": os.environ.get("FAKE_CLAUDE_TRANSCRIPT", ""), **extra}
        if event == "SessionStart":
            data.setdefault("source", "startup")
        started = time.monotonic()
        subprocess.run(command, input=json.dumps(data), text=True, env=hook_env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(time.monotonic() - started, flush=True)

    for event in events:
        run(event)
    for line in sys.stdin:
        if line.strip():
            fields = json.loads(line)
            run(fields.pop("hook_event_name"), **fields)
    while True:
        time.sleep(600)


if __name__ == "__main__":
    main(*sys.argv[1:])
