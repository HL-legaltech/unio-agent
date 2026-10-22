"""Runs a script the way `curl -fsSL ... | sh -s -- ...` runs install.sh, for
tests/run.sh: the script arrives on stdin, and the command has a terminal of
its own (a pseudo-terminal), which is its stdout, its stderr and /dev/tty.

Answers are typed as a person would type them: each one only after its prompt
shows, in order. The terminal echoes what is typed unless the script turned
echo off, so a hidden prompt that is not hidden shows its answer in the
transcript. Prints everything the terminal showed and exits with the
command's status (124 when it ran past the timeout).

Usage:
  pty_run.py <script> <answers> -- <command>...
  pty_run.py --no-tty <script> -- <command>...

<answers> is a file of "<prompt><TAB><reply>" lines. With --no-tty the
command runs in a session of its own without a terminal, so /dev/tty cannot
be opened, and its output is not captured.
"""

import os
import pty
import select
import signal
import subprocess
import sys
import threading
import time

TIMEOUT = 60


def no_tty(script, command):
    with open(script, "rb") as stdin:
        sys.exit(subprocess.run(command, stdin=stdin, start_new_session=True).returncode)


def with_tty(script, answers_file, command):
    answers = []
    with open(answers_file, encoding="utf-8") as lines:
        for line in lines:
            line = line.rstrip("\n")
            if line:
                prompt, reply = line.split("\t", 1)
                answers.append((prompt, reply))

    read_end, write_end = os.pipe()
    pid, master = pty.fork()
    if pid == 0:
        os.close(write_end)
        os.dup2(read_end, 0)
        os.close(read_end)
        os.execvp(command[0], command)
    os.close(read_end)

    def feed():
        with open(script, "rb") as source, os.fdopen(write_end, "wb") as sink:
            try:
                sink.write(source.read())
            except BrokenPipeError:
                pass

    threading.Thread(target=feed, daemon=True).start()

    shown = b""
    searched = 0
    status = None
    deadline = time.monotonic() + TIMEOUT
    while True:
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                chunk = b""
            if not chunk and status is not None:
                break
            shown += chunk
        if answers:
            at = shown.find(answers[0][0].encode(), searched)
            if at >= 0:
                searched = at + len(answers[0][0].encode())
                os.write(master, answers.pop(0)[1].encode() + b"\n")
        if status is None:
            done, raw = os.waitpid(pid, os.WNOHANG)
            if done:
                status = os.waitstatus_to_exitcode(raw)
                deadline = min(deadline, time.monotonic() + 2)
        if time.monotonic() > deadline:
            if status is None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                status = 124
            break
    os.close(master)
    sys.stdout.write(shown.decode("utf-8", "replace").replace("\r\n", "\n"))
    sys.exit(status if status >= 0 else 128 - status)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--no-tty"] and args[2:3] == ["--"]:
        no_tty(args[1], args[3:])
    elif args[2:3] == ["--"]:
        with_tty(args[0], args[1], args[3:])
    else:
        sys.exit(__doc__)
