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
# The folder that picks the workspace is never an argument: Claude Code would
# paste it into a shell command line, where a folder name could run code.
# Claude Code runs this helper in the plugin's own folder and without
# CLAUDE_PROJECT_DIR, so the folder is the coding agent's own (see
# unio_agent_dir). When it cannot be known and some folder is tied to a
# workspace, no token goes out: the computer's default could belong to
# another workspace than the folder's, and Unio Bot would act there.

# shellcheck source=lib.sh
. "$(dirname -- "$0")/lib.sh"

case ${1:-} in
  claude-code|codex) client="$1/$(unio_version "$(unio_root)")" ;;
  *) client= ;;
esac

# An inbox run acts with its runner token, never with a person's. Without a
# mapped folder every folder uses the default, so none is looked up.
token=
if [ "${UNIO_INBOX_RUN:-}" = 1 ]; then
  :
elif unio_mapped ''; then
  folder=${CLAUDE_PROJECT_DIR:-}
  [ -n "$folder" ] || folder=$(unio_agent_dir)
  if [ -z "$folder" ] && [ "$(pwd -P)" != "$(unio_root)" ]; then folder=$PWD; fi
  [ -z "$folder" ] || token=$(unio_read_token "$(unio_token_file "$folder")")
else
  token=$(unio_read_token "$(unio_token_file '')")
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
