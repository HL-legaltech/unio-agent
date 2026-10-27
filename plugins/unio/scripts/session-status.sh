#!/bin/sh
# The session status hook: SessionStart, UserPromptSubmit, PostToolUse on
# Unio's tools, Stop and SessionEnd all run it, and it hands the event on
# its stdin to sessions.py, which shows the session on the agent's page in
# Unio (see unio_sessions/hook.py).
#
# The token goes to Python only as the path of its file, resolved for the
# session's folder as mcp-headers.sh resolves it, and the API only from
# ~/.config/unio/api-url (unio_api): neither is ever an argument or a value
# in the environment. Without Python 3.9 or later nothing is reported, and
# /unio:doctor says how to turn the status on. It prints nothing and always
# exits 0, since a hook's failure would reach the person's session.

# shellcheck source=lib.sh
. "$(dirname -- "$0")/lib.sh"

# An inbox run is not a coding session.
[ "${UNIO_INBOX_RUN:-}" = 1 ] && exit 0
python=$(unio_python) || exit 0

file=$(unio_token_file "${CLAUDE_PROJECT_DIR:-$PWD}")
# Python on Windows is a Windows program, which cannot open /c/... paths.
if [ "$UNIO_OS" = windows ] && command -v cygpath >/dev/null 2>&1; then
  file=$(cygpath -w "$file" 2>/dev/null) || exit 0
fi
UNIO_SESSION_TOKEN_FILE=$file
UNIO_SESSION_API=$(unio_api)
export UNIO_SESSION_TOKEN_FILE UNIO_SESSION_API

# UTF-8 mode: Claude Code's event and every state file are UTF-8, and a
# Windows Python would read them in the console's code page.
# shellcheck disable=SC2086 # the launcher and its flag are two words
$python -X utf8 -I "$(dirname -- "$0")/sessions.py" hook 2>/dev/null
exit 0
