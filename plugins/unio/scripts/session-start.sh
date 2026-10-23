#!/bin/sh
# SessionStart, in two modes.
#   notice   Reads local files only: Claude's first answer waits for it. Prints
#            a systemMessage for the person and, for what Claude must act on,
#            fixed additionalContext.
#   refresh  Runs with async: true. At most every 6 hours, asks GET
#            /v1/agents/me about the token and this plugin's version, and keeps
#            only codes, numbers and versions for the next session's notice.
# Nothing a person typed in Unio, and no CHANGELOG text, reaches Claude's
# context through this hook. It always exits 0.

# shellcheck source=lib.sh
. "$(dirname -- "$0")/lib.sh"

[ "${UNIO_INBOX_RUN:-}" = 1 ] && exit 0

root=$(unio_root)
version=$(unio_version "$root")
data=${CLAUDE_PLUGIN_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/unio-plugin}
project=${CLAUDE_PROJECT_DIR:-$(pwd -P)}
workspace=$(unio_workspace "$project")
file=$(unio_token_file "$project")
token=$(unio_read_token "$file")
status=$data/status-${workspace:-default}
cfg=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
  claude_json=$CLAUDE_CONFIG_DIR/.claude.json
else
  claude_json=$HOME/.claude.json
fi
now=$(date +%s)

field() { sed -n "s/^$1=//p" "$status" 2>/dev/null | head -n 1; }

# The last check still speaks for this token and this plugin version: a
# reconnect or an update makes it stale, so its verdict is never repeated.
fresh() {
  [ -r "$status" ] || return 1
  [ -z "$(find "$file" -newer "$status" 2>/dev/null)" ] || return 1
  [ "$(field version)" = "$version" ]
}

# Windows only: tells the header helper which folder this session works in
# (unio_agent_dir), before anything slower runs.
remember_folder() {
  case ${CLAUDE_PID:-} in ''|*[!0-9]*) return 0 ;; esac
  [ -n "${CLAUDE_PROJECT_DIR:-}" ] || return 0
  sessions=$UNIO_STATE/sessions
  (umask 077 && mkdir -p "$sessions" &&
    printf '%s\n' "$CLAUDE_PROJECT_DIR" > "$sessions/$CLAUDE_PID.tmp.$$" &&
    mv -f "$sessions/$CLAUDE_PID.tmp.$$" "$sessions/$CLAUDE_PID") 2>/dev/null
  return 0
}

refresh() {
  if [ "$UNIO_OS" = windows ]; then
    find "$UNIO_STATE/sessions" -type f -mtime +7 -exec rm -f {} + 2>/dev/null
  fi
  if [ -z "$token" ]; then
    rm -f "$status"
    return 0
  fi
  if fresh && [ "$(field http)" != 000 ]; then
    checked=$(field checked)
    case $checked in
      ''|*[!0-9]*) ;;
      *) [ $((now - checked)) -lt 21600 ] && return 0 ;;
    esac
  fi
  (umask 077 && mkdir -p "$data") || return 0
  body=$(umask 077 && mktemp "$data/me.XXXXXX" 2>/dev/null) || return 0
  http=$(unio_get_me "$token" "$body" "claude-code/$version")
  json=$(cat "$body" 2>/dev/null)
  rm -f "$body"

  state='' current='' minimum='' days=''
  if [ "$http" = 200 ]; then
    state=$(unio_json_field "$json" skill status)
    current=$(unio_json_field "$json" skill current)
    minimum=$(unio_json_field "$json" skill minimum)
    days=$(unio_json_field "$json" token expiresInDays)
  fi
  case $state in current|behind|unsupported|unknown) ;; *) state= ;; esac
  unio_is_version "$current" || current=
  unio_is_version "$minimum" || minimum=
  case $days in ''|*[!0-9]*) days= ;; esac

  (umask 077 &&
    printf 'checked=%s\nversion=%s\nhttp=%s\nstatus=%s\ncurrent=%s\nminimum=%s\ndays=%s\n' \
      "$now" "$version" "$http" "$state" "$current" "$minimum" "$days" > "$status.tmp.$$" &&
    mv -f "$status.tmp.$$" "$status") 2>/dev/null
  return 0
}

# A JSON string body: escapes, line breaks as \n, other control bytes dropped.
json_string() {
  tr '\t' ' ' | tr -d '\000-\010\013-\037\177' |
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' |
    awk 'NR > 1 { printf "\\n" } { printf "%s", $0 }'
}

notice() {
  user=
  model=
  nl='
'
  say() { user="$user${user:+$nl}$1"; }
  tell() { model="$model${model:+ }$1"; }

  # The person learns which token a notice is about by its local id, never
  # by a name from the server: the status file keeps none.
  if [ -n "$workspace" ]; then
    whose="do workspace $workspace"
    where="no workspace $workspace"
  else
    whose="deste computador"
    where="neste workspace"
  fi

  if [ -z "$token" ]; then
    if [ -e "$file" ]; then
      say "O arquivo $(unio_tilde "$file") não contém um token do Unio válido: rode /unio:connect. Nunca cole o token no chat."
    elif [ -n "$workspace" ]; then
      say "Falta o token do Unio do workspace $workspace, que esta pasta usa ($(unio_tilde "$file")): rode /unio:connect. Nunca cole o token no chat."
    else
      say "O Unio não está conectado neste computador: rode /unio:connect. Nunca cole o token no chat."
    fi
    tell "The Unio tools have no token on this computer and will fail. If the user wants Unio work, tell them to run /unio:connect. Never ask them to paste a token into the chat."
  elif fresh; then
    case $(field http) in
      401)
        say "O Unio recusou o token $whose na última verificação (revogado, vencido, ou de quem perdeu o acesso): gere outro em Configurações > Pessoal > Agente de código e rode /unio:connect."
        tell "Unio refused this computer's token at the last check, so the Unio tools will fail until the user runs /unio:connect." ;;
      403)
        say "Os agentes do Unio estão desligados $where: um administrador pode ligá-los em Configurações > Sistema > Agentes."
        tell "Unio agents are switched off in this workspace, so the Unio tools will fail." ;;
      200)
        days=$(field days)
        if [ -n "$days" ] && [ "$days" -le 7 ]; then
          say "O token do Unio $whose $(unio_expires_in "$days"): gere outro em Configurações > Pessoal > Agente de código e rode /unio:connect."
        fi
        case $(field status) in
          unsupported)
            minimum=$(field minimum)
            say "Este plugin do Unio ($version) está abaixo da versão mínima que o Unio aceita${minimum:+ ($minimum)}: rode /unio:update e depois /reload-plugins."
            tell "This Unio plugin is below the minimum version Unio accepts, so every Unio tool but get_me refuses until the user runs /unio:update, then /reload-plugins." ;;
          behind)
            latest=$(field current)
            told=$(cat "$data/told-latest" 2>/dev/null)
            if [ -n "$latest" ] && [ "$latest" != "$told" ] && ! unio_version_ge "$version" "$latest"; then
              say "Saiu a versão $latest do plugin do Unio (você tem a $version): rode /unio:update, ou ligue a atualização automática em /plugin > Marketplaces > unio."
              (umask 077 && printf '%s' "$latest" > "$data/told-latest") 2>/dev/null
            fi ;;
        esac ;;
    esac
  fi

  seen=$(cat "$data/seen-version" 2>/dev/null)
  if [ -n "$seen" ] && [ "$seen" != "$version" ]; then
    say "Plugin do Unio atualizado para $version."
    changes=$(awk '/^## /{n++; next} n==1' "$root/CHANGELOG.md" 2>/dev/null | sed '/^[[:space:]]*$/d' | head -n 8)
    [ -z "$changes" ] || say "O que mudou:$nl$changes"
  fi
  if [ "$seen" != "$version" ]; then
    (umask 077 && mkdir -p "$data" && printf '%s' "$version" > "$data/seen-version") 2>/dev/null
  fi

  if [ -e "$cfg/skills/unio/SKILL.md" ]; then
    say "Uma skill unio instalada à mão ($(unio_tilde "$cfg/skills/unio")) também está carregada: rode /unio:doctor para ver como retirá-la."
  fi
  if grep -q 'api\.unio\.so/v1/mcp' "$claude_json" 2>/dev/null; then
    say "Uma entrada do servidor do Unio feita à mão em $(unio_tilde "$claude_json") tem precedência sobre a do plugin: rode /unio:doctor para ver como removê-la."
  fi

  [ -n "$user" ] || return 0
  printf '{"systemMessage":"%s"' "$(printf '%s' "$user" | json_string)"
  if [ -n "$model" ]; then
    printf ',"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}' \
      "$(printf '%s' "$model" | json_string)"
  fi
  printf '}\n'
}

case ${1:-notice} in
  refresh) refresh ;;
  *)
    [ "$UNIO_OS" != windows ] || remember_folder
    notice ;;
esac
exit 0
