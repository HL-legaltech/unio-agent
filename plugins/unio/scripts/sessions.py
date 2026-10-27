"""Coding sessions on the agent's page in Unio. session-status.sh runs it for
Claude Code's hooks; bin/unio and bin/unio.cmd run it for `unio resume`.

    sessions.py hook                      one hook event, as JSON on stdin
    sessions.py watch --session <id> ...  the session's watcher (detached)
    sessions.py send-presence <id> <seq>  one presence report (detached)
    sessions.py resume <task|id> [--list|--print]

It runs under python -I, which leaves this folder off sys.path, so it puts it
there itself. A hook always exits 0.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main(argv):
    command, rest = (argv[0], argv[1:]) if argv else ("", [])
    if command == "resume":
        # Its messages are Portuguese: a pipe on Windows would get them in
        # the console's code page, whoever started it and with what flags.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        from unio_sessions import resume
        return resume.main(rest)
    try:
        if command == "hook":
            from unio_sessions import hook
            return hook.main(sys.stdin)
        if command == "send-presence":
            from unio_sessions import hook
            return hook.main_send(rest)
        if command == "watch":
            from unio_sessions import watch
            return watch.main(rest)
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
