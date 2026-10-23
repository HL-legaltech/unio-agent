# Sourced by the plugin's scripts. POSIX sh and utilities, plus curl, mktemp
# and date +%s: they run under dash, bash, macOS sh and Git Bash.

# Byte semantics for every pattern and length below; output is unaffected.
LC_ALL=C
export LC_ALL

UNIO_CONF=$HOME/.config/unio
UNIO_STATE=${XDG_STATE_HOME:-$HOME/.local/state}/unio

# linux (WSL included), macos, or windows: Git Bash, MSYS2 or Cygwin, where
# Claude Code runs the plugin's scripts on Windows.
case $(uname -s 2>/dev/null) in
  Darwin) UNIO_OS=macos ;;
  MINGW*|MSYS*|CYGWIN*) UNIO_OS=windows ;;
  *) UNIO_OS=linux ;;
esac

# The API is pinned to api.unio.so. Only ~/.config/unio/api-url moves it (a
# test or staging API), never an environment variable: any program a session
# starts can set one, and the token would follow it.
unio_api() {
  origin=https://api.unio.so
  if [ -r "$UNIO_CONF/api-url" ]; then
    o=$(head -n 1 "$UNIO_CONF/api-url" | tr -d ' \t\r')
    while case $o in */) true ;; *) false ;; esac; do o=${o%/}; done
    case $o in
      *[!A-Za-z0-9.:/-]*) ;;
      https://?*|http://127.0.0.1:*|http://localhost:*) origin=$o ;;
    esac
  fi
  printf '%s/v1' "$origin"
}

# A path as people read it, with ~ for the home folder.
unio_tilde() {
  # shellcheck disable=SC2088 # the tilde is printed, never expanded
  case $1 in "$HOME"/*) printf '~/%s' "${1#"$HOME"/}" ;; *) printf '%s' "$1" ;; esac
}

unio_root() {
  CDPATH='' cd -- "$(dirname -- "$0")/.." 2>/dev/null && pwd -P
}

# MAJOR.MINOR or MAJOR.MINOR.PATCH, 1 to 3 digits each: the only shape the
# server accepts in X-Unio-Client and the only one these scripts repeat.
unio_is_version() {
  case $1 in ''|*[!0-9.]*|.*|*.|*..*|*.*.*.*) return 1 ;; esac
  case $1 in *.*) ;; *) return 1 ;; esac
  for part in $(printf '%s' "$1" | tr . ' '); do
    [ "${#part}" -le 3 ] || return 1
  done
}

# The plugin's own version, from the manifest the release pins.
unio_version() {
  v=$(sed -n 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$1/.claude-plugin/plugin.json" 2>/dev/null | head -n 1)
  unio_is_version "$v" || v=0.0.0
  printf '%s' "$v"
}

# True when version $1 >= version $2.
unio_version_ge() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    n = split(a, x, "."); m = split(b, y, ".")
    for (i = 1; i <= (n > m ? n : m); i++) {
      if ((x[i] + 0) > (y[i] + 0)) exit 0
      if ((x[i] + 0) < (y[i] + 0)) exit 1
    }
    exit 0
  }'
}

# A workspace key is its Clerk organization id; it becomes a folder name.
unio_is_org() {
  case $1 in org_?*) ;; *) return 1 ;; esac
  case ${1#org_} in *[!A-Za-z0-9]*) return 1 ;; esac
}

# Folder $1 in the one form folders are compared in. Claude Code names a
# Windows folder C:/x, a person may type C:\x, and Git Bash's pwd -P, which
# the installer writes to ~/.config/unio/projects, says /c/x. Windows and
# macOS ignore case, so there the form is lower case.
unio_dir_key() {
  d=$1
  # A folder that exists is read back from the system, physical and, on
  # Windows, by its long name: Claude Code may name it C:/Users/RUNNER~1/...
  # where Git Bash says /c/Users/runneradmin/....
  real=$(CDPATH='' cd -P -- "$d" 2>/dev/null && pwd -P) && [ -n "$real" ] && d=$real
  if [ "$UNIO_OS" = windows ]; then
    case $d in
      [A-Za-z]:*)
        if command -v cygpath >/dev/null 2>&1; then
          d=$(cygpath -u "$d" 2>/dev/null) || d=$1
        else
          drive=$(printf '%s' "${d%%:*}" | tr '[:upper:]' '[:lower:]')
          d=/$drive$(printf '%s' "${d#??}" | tr '\134' /)
        fi ;;
    esac
  fi
  while case $d in ?*/) true ;; *) false ;; esac; do d=${d%/}; done
  case $UNIO_OS in
    windows|macos) d=$(printf '%s' "$d" | tr '[:upper:]' '[:lower:]') ;;
  esac
  printf '%s' "$d"
}

# The workspace a folder works in: the longest folder in
# ~/.config/unio/projects ("<folder><TAB><org id>" per line) that holds it,
# else the one in ~/.config/unio/default-workspace. Prints nothing when
# neither applies, which means the single token in ~/.config/unio/token.
unio_workspace() {
  org=
  if [ -n "$1" ] && [ -r "$UNIO_CONF/projects" ]; then
    best=
    want=$(unio_dir_key "$1")
    tab=$(printf '\t')
    while IFS=$tab read -r path key || [ -n "$path" ]; do
      # A line saved on Windows ends in CR, which would fail unio_is_org and
      # drop the mapping without a word, sending another workspace's token.
      key=${key#"${key%%[![:space:]]*}"}
      key=${key%"${key##*[![:space:]]}"}
      path=${path%"${path##*[![:space:]]}"}
      if [ -z "$path" ] || ! unio_is_org "$key"; then continue; fi
      path=$(unio_dir_key "$path")
      [ -n "$path" ] || continue
      case $want/ in
        "$path"/*)
          if [ "${#path}" -gt "${#best}" ]; then
            best=$path
            org=$key
          fi
          ;;
      esac
    done < "$UNIO_CONF/projects"
  fi
  if [ -z "$org" ] && [ -r "$UNIO_CONF/default-workspace" ]; then
    key=$(head -n 1 "$UNIO_CONF/default-workspace" | tr -d ' \t\r')
    unio_is_org "$key" && org=$key
  fi
  printf '%s' "$org"
}

unio_token_file() {
  org=$(unio_workspace "$1")
  if [ -n "$org" ]; then
    printf '%s/workspaces/%s/token' "$UNIO_CONF" "$org"
  else
    printf '%s/token' "$UNIO_CONF"
  fi
}

# The process id of the coding agent this script serves: its nearest
# ancestor that is neither a shell nor git, which runs the header helper for
# Claude Code (see .mcp.json); Codex starts it directly.
unio_agent_pid() {
  pid=$PPID
  hops=0
  while [ "$hops" -lt 6 ]; do
    case $pid in ''|0|1|*[!0-9]*) return 1 ;; esac
    name=$(ps -o comm= -p "$pid" 2>/dev/null) || return 1
    name=${name%"${name##*[![:space:]]}"}
    name=${name##*/}
    case ${name#-} in
      '') return 1 ;;
      sh|bash|dash|zsh|ksh|mksh|ash|busybox|env|timeout|nice|git) ;;
      *) printf '%s' "$pid"; return 0 ;;
    esac
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    hops=$((hops + 1))
  done
  return 1
}

# Windows lets no process read another one's folder. So there the session
# hook writes Claude Code's folder to $UNIO_STATE/sessions/<CLAUDE_PID>, and
# the header helper, which Claude Code starts without CLAUDE_PID, asks Windows
# for its own nearest ancestor that is not a shell. Prints "<pid> <start>
# <name>", the start in seconds since 1970.
unio_windows_agent() {
  self=$(cat "/proc/$$/winpid" 2>/dev/null)
  case $self in ''|*[!0-9]*) return 1 ;; esac
  # shellcheck disable=SC2016 # PowerShell's variables, never the shell's
  MSYS_NO_PATHCONV=1 powershell.exe -NoProfile -NonInteractive -Command '
    $p = '"$self"'
    for ($i = 0; $i -lt 8; $i++) {
      $w = Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue
      if (-not $w) { exit 1 }
      if ($w.Name -notmatch "^(sh|bash|dash|zsh|env|timeout|git|cmd|powershell|pwsh|conhost)(\.exe)?$") {
        $start = [int64]($w.CreationDate.ToUniversalTime() - [datetime]"1970-01-01").TotalSeconds
        Write-Output "$($w.ProcessId) $start $($w.Name)"
        exit 0
      }
      $p = $w.ParentProcessId
    }
    exit 1' 2>/dev/null | tr -d '\r' | head -n 1
}

# The session file the hook wrote for Claude Code process $1.
unio_session_file() {
  printf '%s/sessions/%s' "$UNIO_STATE" "$1"
}

# The folder the coding agent works in, or nothing when it cannot be known.
# Claude Code runs the header helper in the plugin's own folder and passes no
# project, so the folder is read off the agent's process.
unio_agent_dir() {
  case $UNIO_OS in
    linux)
      pid=$(unio_agent_pid) || return 0
      (CDPATH='' cd -P -- "/proc/$pid/cwd" 2>/dev/null && pwd -P) ;;
    macos)
      pid=$(unio_agent_pid) || return 0
      lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 ;;
    windows)
      agent=$(unio_windows_agent)
      pid=${agent%% *}
      started=${agent#* }
      name=${started#* }
      started=${started%% *}
      case $pid$started in ''|*[!0-9]*) return 0 ;; esac
      # Only Claude Code has a hook that leaves the file; Codex never will.
      case $name in [Cc]laude*) ;; *) return 0 ;; esac
      file=$(unio_session_file "$pid")
      # The hook may still be running: Claude Code connects to Unio while it
      # starts its hooks. A file older than the process is a past session's.
      tries=0
      while [ "$tries" -lt 20 ]; do
        written=$(date -r "$file" +%s 2>/dev/null)
        if [ -n "$written" ] && [ "$written" -ge $((started - 1)) ]; then
          head -n 1 "$file"
          return 0
        fi
        sleep 0.25
        tries=$((tries + 1))
      done ;;
  esac
  return 0
}

# The installer's command that replaces workspace $1's token and installs
# nothing, as the Agente de código page shows it for a renewal.
unio_renew_command() {
  printf 'curl -fsSL https://raw.githubusercontent.com/HL-legaltech/unio-agent/main/install.sh | sh -s -- --workspace %s --renew' "$1"
}

unio_expires_in() {
  case $1 in
    0) printf 'vence hoje' ;;
    1) printf 'vence em 1 dia' ;;
    *) printf 'vence em %s dias' "$1" ;;
  esac
}

# unio_ followed by 46 base62 characters, as the server mints them.
unio_is_token() {
  case $1 in unio_*) ;; *) return 1 ;; esac
  case ${1#unio_} in *[!0-9A-Za-z]*) return 1 ;; esac
  [ "${#1}" -eq 51 ]
}

# Prints the token in file $1, or nothing when the file is missing or holds
# anything else. tr reads the file on stdin, so the token is never an argument.
unio_read_token() {
  [ -r "$1" ] || return 0
  t=$(tr -d '\r\n' < "$1")
  if unio_is_token "$t"; then printf '%s' "$t"; fi
  return 0
}

# GET /agents/me with token $1, body to file $2, X-Unio-Client $3 when set.
# curl reads the headers from stdin (-K -), so the token never shows in a
# process list. Prints the HTTP status, 000 when nothing answered.
unio_get_me() {
  code=$(
    {
      printf 'header = "Authorization: Bearer %s"\n' "$1"
      if [ -n "${3:-}" ]; then printf 'header = "X-Unio-Client: %s"\n' "$3"; fi
    } | curl -sS --max-time 15 -K - -o "$2" -w '%{http_code}' "$(unio_api)/agents/me" 2>/dev/null
  )
  case $code in [0-9][0-9][0-9]) ;; *) code=000 ;; esac
  printf '%s' "$code"
}

# A field of a flat object in the API's JSON, e.g. unio_json_field "$body"
# skill status. Only for objects the API keeps flat (no nested braces); a
# string value that holds a brace reads as missing, never as another field.
unio_json_field() {
  s='[[:space:]]*'
  flat=$(printf '%s' "$1" | tr -d '\n')
  v=$(printf '%s' "$flat" | sed -n "s/.*\"$2\"$s:$s{[^}]*\"$3\"$s:$s\"\([^\"]*\)\".*/\1/p" | head -n 1)
  [ -n "$v" ] || v=$(printf '%s' "$flat" | sed -n "s/.*\"$2\"$s:$s{[^}]*\"$3\"$s:$s\([^\",} ]*\).*/\1/p" | head -n 1)
  printf '%s' "$v"
}

# The browser approval and the saving of its token below have copies in
# install.sh, which cannot source this file: change both together.

# POSTs the JSON on stdin to path $1 of the API, body to file $2; prints the
# HTTP status, 000 when nothing answered. The JSON travels on stdin, so a
# device code never shows in a process list.
unio_post() {
  code=$(curl -sS --max-time 15 -X POST -H 'Content-Type: application/json' \
    --data-binary @- -o "$2" -w '%{http_code}' "$(unio_api)$1" 2>/dev/null)
  case $code in [0-9][0-9][0-9]) ;; *) code=000 ;; esac
  printf '%s' "$code"
}

# The value of key $2 in JSON $1, a string or a number, and only when it is
# made of characters a URL, a code or a number needs: nothing a terminal
# would read as a control sequence.
unio_json_value() {
  printf '%s' "$1" | tr -d '\n' |
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([A-Za-z0-9_:/?=.&-]*\)[\",}].*/\1/p" | head -n 1
}

# This computer's short name, as the approval page shows it and the
# connection is named: a hostname's characters only.
unio_host() {
  h=$(uname -n 2>/dev/null | cut -d. -f1 | tr -cd 'A-Za-z0-9_-' | cut -c1-64)
  case $h in [A-Za-z0-9]*) ;; *) h=computador ;; esac
  printf '%s' "$h"
}

# Opens URL $1 in a browser on this screen, or does nothing: the URL is
# printed anyway. Over SSH, or without a display, xdg-open would start a
# text browser in this very terminal; on Linux, open is openvt.
unio_open_url() {
  [ -z "${SSH_CONNECTION:-}" ] || return 0
  case $(uname -s 2>/dev/null) in
    Darwin)
      if command -v open >/dev/null 2>&1; then (open "$1" </dev/null >/dev/null 2>&1 &); fi ;;
    MINGW*|MSYS*|CYGWIN*)
      (rundll32.exe url.dll,FileProtocolHandler "$1" </dev/null >/dev/null 2>&1 &) ;;
    *)
      # WSL opens the browser of the Windows around it, through interop.
      if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null &&
        command -v rundll32.exe >/dev/null 2>&1; then
        (rundll32.exe url.dll,FileProtocolHandler "$1" </dev/null >/dev/null 2>&1 &)
      elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
        (xdg-open "$1" </dev/null >/dev/null 2>&1 &)
      fi ;;
  esac
  return 0
}

# Asks Unio for a token for client $1 and waits while a person approves the
# request in the browser. On success sets token, owner (the workspace's org
# id), owner_name and days; otherwise says why and returns 1. Neither the
# token nor the device code is ever printed or put on a command line.
unio_connect_browser() {
  token=
  reply=$(umask 077 && mktemp 2>/dev/null) || reply=
  if [ -z "$reply" ]; then
    echo "Não foi possível criar um arquivo temporário. Nada foi salvo."
    return 1
  fi
  trap 'rm -f "$reply"' EXIT
  trap 'rm -f "$reply"; printf "\nCancelado. Nada foi salvo.\n"; exit 130' INT TERM HUP
  status=$(printf '{"client":"%s","host":"%s"}' "$1" "$(unio_host)" | unio_post /agents/connect/start "$reply")
  started=$(cat "$reply" 2>/dev/null)
  case $status in
    201) ;;
    000) echo "O Unio não respondeu: confira a rede e tente de novo. Nada foi salvo."; return 1 ;;
    404) echo "A aprovação pelo navegador ainda não está disponível. Gere um token em Configurações > Pessoal > Agente de código, no Unio, e rode o comando que a página mostra."; return 1 ;;
    429) echo "Muitos pedidos de conexão deste endereço. Espere um minuto e tente de novo."; return 1 ;;
    *) echo "O Unio respondeu HTTP $status. Nada foi salvo; tente de novo em instantes."; return 1 ;;
  esac
  device=$(unio_json_value "$started" deviceCode)
  user_code=$(unio_json_value "$started" userCode)
  url=$(unio_json_value "$started" verifyUrl)
  interval=$(unio_json_value "$started" interval)
  ttl=$(unio_json_value "$started" expiresIn)
  case $interval in ''|*[!0-9]*) interval=5 ;; esac
  [ "$interval" -ge 1 ] && [ "$interval" -le 60 ] || interval=5
  case $ttl in ''|*[!0-9]*) ttl=600 ;; esac
  case $user_code in [A-Z][A-Z][A-Z][A-Z]-[A-Z][A-Z][A-Z][A-Z]) ;; *) user_code= ;; esac
  case $url in https://?*|http://127.0.0.1:*|http://localhost:*) ;; *) url= ;; esac
  if [ "${#device}" -ne 43 ] || [ -z "$user_code" ] || [ -z "$url" ]; then
    echo "O Unio respondeu de um jeito inesperado. Nada foi salvo; tente de novo em instantes."
    return 1
  fi

  echo "Abra esta página no navegador, entre no Unio e confira o código $user_code:"
  echo
  echo "  $url"
  echo
  echo "Escolha o workspace na página e aprove. Esperando a aprovação por até $((ttl / 60)) minutos; Ctrl+C cancela."
  unio_open_url "$url"

  deadline=$(($(date +%s) + ttl))
  while :; do
    sleep "$interval"
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "O pedido venceu sem aprovação. Nada foi salvo; rode o comando de novo."
      return 1
    fi
    status=$(printf '{"deviceCode":"%s"}' "$device" | unio_post /agents/connect/poll "$reply")
    case $status in
      200) break ;;
      202|000) ;;
      429) interval=$((interval + 5)) ;;
      403) echo "O pedido foi recusado no navegador. Nada foi salvo."; return 1 ;;
      410) echo "O pedido venceu ou já foi usado. Nada foi salvo; rode o comando de novo."; return 1 ;;
      *) echo "O Unio respondeu HTTP $status. Nada foi salvo; rode o comando de novo."; return 1 ;;
    esac
  done
  token=$(sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\(unio_[0-9A-Za-z]*\)".*/\1/p' "$reply" | head -n 1)
  approved=$(sed 's/"token"[[:space:]]*:[[:space:]]*"[^"]*"//' "$reply")
  rm -f "$reply"
  owner=$(unio_json_field "$approved" workspace clerkId)
  # shellcheck disable=SC2034 # owner_name and days are the caller's
  owner_name=$(unio_json_field "$approved" workspace name | tr -d '\000-\037\177' | cut -c1-80)
  # shellcheck disable=SC2034
  days=$(unio_json_field "$approved" connection expiresInDays)
  if ! unio_is_token "$token" || ! unio_is_org "$owner"; then
    token=
    echo "O Unio aprovou, mas a resposta veio incompleta. Nada foi salvo; revogue a conexão nova em Configurações > Pessoal > Agente de código e rode o comando de novo."
    return 1
  fi
}

# True when a line of ~/.config/unio/projects maps some folder to workspace
# $1, or to any workspace when $1 is empty.
unio_mapped() {
  [ -r "$UNIO_CONF/projects" ] || return 1
  tab=$(printf '\t')
  while IFS=$tab read -r path key || [ -n "$path" ]; do
    key=${key#"${key%%[![:space:]]*}"}
    key=${key%"${key##*[![:space:]]}"}
    path=${path%"${path##*[![:space:]]}"}
    if [ -n "$path" ] && unio_is_org "$key" && { [ -z "$1" ] || [ "$key" = "$1" ]; }; then
      return 0
    fi
  done < "$UNIO_CONF/projects"
  return 1
}

# Whether the computer's own token (~/.config/unio/token, read while no
# default-workspace exists) serves another workspace than $1, or one Unio
# cannot name. A token Unio refuses serves nothing.
unio_legacy_elsewhere() {
  t=$(unio_read_token "$UNIO_CONF/token")
  [ -n "$t" ] || return 1
  b=$(umask 077 && mktemp 2>/dev/null) || return 0
  answer=$(unio_get_me "$t" "$b")
  holder=$(unio_json_field "$(cat "$b" 2>/dev/null)" workspace clerkId)
  rm -f "$b"
  case $answer in
    401) return 1 ;;
    200) [ "$holder" != "$1" ] ;;
    *) return 0 ;;
  esac
}

# Sets scope to folder or default, asking on the terminal; without one, the
# folder, the narrower choice.
unio_ask_scope() {
  scope=folder
  if ! ( : </dev/tty ) 2>/dev/null; then
    echo "Este computador já usa o Unio com outro workspace, então o workspace $workspace vale só nesta pasta."
    return 0
  fi
  {
    echo "Este computador já usa o Unio com outro workspace. Onde usar o workspace $workspace?"
    echo "  1. Só nesta pasta: $(unio_tilde "$here")"
    echo "  2. Em todo o computador, menos nas pastas ligadas a outro workspace"
  } >/dev/tty
  while :; do
    printf 'Escolha 1 ou 2 [1]: ' >/dev/tty
    IFS= read -r choice </dev/tty || choice=1
    case $choice in
      ''|1) scope=folder; return 0 ;;
      2) scope=default; return 0 ;;
    esac
  done
}

# Maps this folder to the workspace in ~/.config/unio/projects, both as the
# system names it and as the shell reached it when a link is on the way.
unio_map_folder() {
  tab=$(printf '\t')
  nl='
'
  case $here$logical in *"$tab"*|*"$nl"*) return 1 ;; esac
  {
    if [ -r "$UNIO_CONF/projects" ]; then
      A=$here B=$logical awk -F "$tab" '{
        p = $1; sub(/[[:space:]]+$/, "", p)
        while (p ~ /.\/$/) sub(/\/$/, "", p)
        if (p != ENVIRON["A"] && p != ENVIRON["B"]) print
      }' "$UNIO_CONF/projects"
    fi
    printf '%s\t%s\n' "$here" "$workspace"
    if [ -n "$logical" ] && [ "$logical" != "$here" ]; then printf '%s\t%s\n' "$logical" "$workspace"; fi
  } > "$UNIO_CONF/projects.new" && mv -f "$UNIO_CONF/projects.new" "$UNIO_CONF/projects"
}

# Saves token $1 for workspace $2 (named $3, $4 days left) as the installer
# does: ~/.config/unio/workspaces/<org>/token through a rename, the first
# workspace as the computer's default, another one for this folder unless
# the person picks the whole computer.
unio_store_token() {
  workspace=$2
  here=$(pwd -P)
  logical=$(pwd -L 2>/dev/null)
  default=$(unio_workspace '')
  if [ "$(unio_workspace "$here")" = "$workspace" ] || [ "$(unio_workspace "$logical")" = "$workspace" ]; then
    scope=kept
  elif [ -z "$default" ] && ! unio_mapped '' && ! unio_legacy_elsewhere "$workspace"; then
    scope=first
  else
    unio_ask_scope
  fi

  file=$UNIO_CONF/workspaces/$workspace/token
  if ! { (umask 077 && mkdir -p "$UNIO_CONF/workspaces/$workspace") &&
    chmod 700 "$UNIO_CONF" "$UNIO_CONF/workspaces" "$UNIO_CONF/workspaces/$workspace" &&
    (umask 077 && printf '%s' "$1" > "$file.new") && mv -f "$file.new" "$file"; } 2>/dev/null; then
    rm -f "$file.new"
    echo "Não foi possível gravar $(unio_tilde "$file"). Nada foi salvo."
    return 1
  fi
  printf 'Token salvo em %s' "$(unio_tilde "$file")"
  [ -z "$3" ] || printf ', para o workspace %s' "$3"
  case $4 in ''|*[!0-9]*) printf '.\n' ;; *) printf '. Ele %s.\n' "$(unio_expires_in "$4")" ;; esac

  case $scope in
    first|default)
      if ! { (umask 077 && printf '%s\n' "$workspace" > "$UNIO_CONF/default-workspace.new") &&
        mv -f "$UNIO_CONF/default-workspace.new" "$UNIO_CONF/default-workspace"; } 2>/dev/null; then
        rm -f "$UNIO_CONF/default-workspace.new"
        echo "Não foi possível gravar $(unio_tilde "$UNIO_CONF/default-workspace"), então nenhuma pasta usa este token ainda."
        return 1
      fi
      if [ "$scope" = first ]; then
        echo "O workspace $workspace vale em todas as pastas deste computador."
      else
        echo "O workspace $workspace passa a valer em todas as pastas deste computador, menos nas ligadas a outro workspace em $(unio_tilde "$UNIO_CONF/projects")."
      fi
      if [ -z "$default" ] && [ -e "$UNIO_CONF/token" ]; then
        echo "O token em $(unio_tilde "$UNIO_CONF/token") deixa de ser lido."
      fi ;;
    folder)
      if ! (umask 077 && unio_map_folder) 2>/dev/null; then
        rm -f "$UNIO_CONF/projects.new"
        echo "Não foi possível ligar esta pasta ao workspace em $(unio_tilde "$UNIO_CONF/projects"), então nenhuma pasta usa este token ainda."
        return 1
      fi
      echo "O workspace $workspace vale só em $(unio_tilde "$here") e nas pastas dentro dela." ;;
  esac
}
