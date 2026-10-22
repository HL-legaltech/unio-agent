#!/bin/sh
# Claude Code's headersHelper and Codex's http_headers_helper. Both run it on
# every connection to Unio's MCP server, and again after a 401 or 403, and
# read one JSON object of headers from stdout within 10 seconds.
#
# It always prints valid JSON. Without a well-formed token it sends only
# X-Unio-Client, so the server answers 401 and /unio:doctor can say why. The
# token reaches stdout through the printf builtin, never through an argv.
#
# Usage: mcp-headers.sh <claude-code|codex>
#
# The folder that picks the workspace is $CLAUDE_PROJECT_DIR, else the working
# directory. It is never an argument: Claude Code would paste it into a shell
# command line, where a folder name could run code.

# shellcheck source=lib.sh
. "$(dirname -- "$0")/lib.sh"

case ${1:-} in
  claude-code|codex) client="$1/$(unio_version "$(unio_root)")" ;;
  *) client= ;;
esac

# An inbox run acts with its runner token, never with a person's.
token=
if [ "${UNIO_INBOX_RUN:-}" != 1 ]; then
  token=$(unio_read_token "$(unio_token_file "${CLAUDE_PROJECT_DIR:-$PWD}")")
fi

if [ -n "$token" ] && [ -n "$client" ]; then
  printf '{"Authorization":"Bearer %s","X-Unio-Client":"%s"}\n' "$token" "$client"
elif [ -n "$token" ]; then
  printf '{"Authorization":"Bearer %s"}\n' "$token"
elif [ -n "$client" ]; then
  printf '{"X-Unio-Client":"%s"}\n' "$client"
else
  printf '{}\n'
fi
exit 0
