#!/bin/sh
# Installs the Unio plugin and stores a Unio token for one workspace. It is the
# command the Agente de código page in Unio shows:
#
#   curl -fsSL https://raw.githubusercontent.com/HL-legaltech/unio-agent/main/install.sh |
#     sh -s -- [--workspace org_<id>] [--client claude-code|codex] [--renew] [--browser]
#
#   --workspace  The workspace the token belongs to (its Clerk organization id),
#                for a token pasted at a hidden prompt.
#   --client     claude-code (default) installs the plugin; codex clones this
#                repository and links the skill.
#   --renew      Only replaces the token: nothing is installed.
#   --browser    Asks Unio for the token and waits while you approve it in the
#                browser, where you also pick the workspace. The default
#                without --workspace.
#
# It runs piped into sh, so it cannot source the plugin's lib.sh, and its stdin
# is this script: the token and every answer are read from /dev/tty. All of it
# runs from main, on the last line, so a download cut short runs nothing.

LC_ALL=C
export LC_ALL
umask 077

conf=$HOME/.config/unio
marketplace=HL-legaltech/unio-agent
case $(uname -s 2>/dev/null) in
  Darwin) os=macos ;;
  MINGW*|MSYS*|CYGWIN*) os=windows ;;
  *) os=linux ;;
esac
# plugin.json's minimumClaudeCode: the first release with install --marketplace.
floor=2.1.292
clone=$HOME/.local/share/unio/agent

say() { printf '%s\n' "$1"; }

# Text a person typed or a server sent, safe to print on a terminal.
clean() { printf '%s' "$1" | tr -d '\000-\037\177' | cut -c1-80; }

# A path as people read it, with ~ for the home folder.
tilde() {
  # shellcheck disable=SC2088 # the tilde is printed, never expanded
  case $1 in "$HOME"/*) printf '~/%s' "${1#"$HOME"/}" ;; *) printf '%s' "$1" ;; esac
}

# The API is pinned to api.unio.so; only ~/.config/unio/api-url moves it, as
# in the plugin's lib.sh.
api() {
  origin=https://api.unio.so
  if [ -r "$conf/api-url" ]; then
    o=$(head -n 1 "$conf/api-url" | tr -d ' \t\r')
    while case $o in */) true ;; *) false ;; esac; do o=${o%/}; done
    case $o in
      *[!A-Za-z0-9.:/-]*) ;;
      https://?*|http://127.0.0.1:*|http://localhost:*) origin=$o ;;
    esac
  fi
  printf '%s/v1' "$origin"
}

is_org() {
  case $1 in org_?*) ;; *) return 1 ;; esac
  case ${1#org_} in *[!A-Za-z0-9]*) return 1 ;; esac
}

is_token() {
  case $1 in unio_*) ;; *) return 1 ;; esac
  case ${1#unio_} in *[!0-9A-Za-z]*) return 1 ;; esac
  [ "${#1}" -eq 51 ]
}

# True when version $1 >= version $2.
version_ge() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    n = split(a, x, "."); m = split(b, y, ".")
    for (i = 1; i <= (n > m ? n : m); i++) {
      if ((x[i] + 0) > (y[i] + 0)) exit 0
      if ((x[i] + 0) < (y[i] + 0)) exit 1
    }
    exit 0
  }'
}

expires_in() {
  case $1 in
    0) printf 'vence hoje' ;;
    1) printf 'vence em 1 dia' ;;
    *) printf 'vence em %s dias' "$1" ;;
  esac
}

# A field of a flat object in the API's JSON, as lib.sh reads it.
json_field() {
  s='[[:space:]]*'
  flat=$(printf '%s' "$1" | tr -d '\n')
  v=$(printf '%s' "$flat" | sed -n "s/.*\"$2\"$s:$s{[^}]*\"$3\"$s:$s\"\([^\"]*\)\".*/\1/p" | head -n 1)
  [ -n "$v" ] || v=$(printf '%s' "$flat" | sed -n "s/.*\"$2\"$s:$s{[^}]*\"$3\"$s:$s\([^\",} ]*\).*/\1/p" | head -n 1)
  printf '%s' "$v"
}

# GET /agents/me with token $1, body to file $2; prints the HTTP status, 000
# when nothing answered. curl reads the header from stdin (-K -), so the token
# never shows in a process list. No X-Unio-Client: the page counts a request
# with it as the coding agent's first contact, which this check is not.
get_me() {
  code=$(
    printf 'header = "Authorization: Bearer %s"\n' "$1" |
      curl -sS --max-time 15 -K - -o "$2" -w '%{http_code}' "$(api)/agents/me" 2>/dev/null
  )
  case $code in [0-9][0-9][0-9]) ;; *) code=000 ;; esac
  printf '%s' "$code"
}

default_org() {
  [ -r "$conf/default-workspace" ] || return 0
  key=$(head -n 1 "$conf/default-workspace" | tr -d ' \t\r')
  if is_org "$key"; then printf '%s' "$key"; fi
}

# Folder $1 in the one form folders are compared in, as lib.sh's
# unio_dir_key writes it.
dir_key() {
  d=$1
  if [ "$os" = windows ]; then
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
  case $os in
    windows|macos) d=$(printf '%s' "$d" | tr '[:upper:]' '[:lower:]') ;;
  esac
  printf '%s' "$d"
}

# Each line of ~/.config/unio/projects that lib.sh would use, as
# "<folder><TAB><org>", trimmed as lib.sh trims it.
project_lines() {
  [ -r "$conf/projects" ] || return 0
  tab=$(printf '\t')
  while IFS=$tab read -r path key || [ -n "$path" ]; do
    key=${key#"${key%%[![:space:]]*}"}
    key=${key%"${key##*[![:space:]]}"}
    path=${path%"${path##*[![:space:]]}"}
    while case $path in ?*/) true ;; *) false ;; esac; do path=${path%/}; done
    if [ -n "$path" ] && is_org "$key"; then printf '%s\t%s\n' "$path" "$key"; fi
  done < "$conf/projects"
}

# The workspace folder $1 works in, as the plugin's header helper picks it:
# the longest mapped folder that holds it, else default-workspace.
workspace_of() {
  org=
  best=
  want=$(dir_key "$1")
  tab=$(printf '\t')
  lines=$(project_lines)
  while IFS=$tab read -r path key; do
    [ -n "$path" ] || continue
    path=$(dir_key "$path")
    case $want/ in
      "$path"/*) if [ "${#path}" -gt "${#best}" ]; then best=$path; org=$key; fi ;;
    esac
  done <<EOF
$lines
EOF
  [ -n "$org" ] || org=$(default_org)
  printf '%s' "$org"
}

# True when a projects line maps some folder to workspace $1, or to any
# workspace when $1 is empty.
mapped() {
  tab=$(printf '\t')
  lines=$(project_lines)
  while IFS=$tab read -r path key; do
    [ -n "$path" ] || continue
    if [ -z "$1" ] || [ "$key" = "$1" ]; then return 0; fi
  done <<EOF
$lines
EOF
  return 1
}

need_claude() {
  if ! command -v claude >/dev/null 2>&1; then
    say "O comando claude não está no PATH. Instale o Claude Code $floor ou mais novo e rode este comando de novo."
    return 1
  fi
  cc=$(claude --version </dev/null 2>/dev/null | sed -n 's/^\([0-9][0-9]*\.[0-9][0-9.]*\).*/\1/p' | head -n 1)
  if [ -z "$cc" ]; then
    say "Não foi possível ler a versão do Claude Code (claude --version). Confira a instalação e rode este comando de novo."
    return 1
  fi
  if ! version_ge "$cc" "$floor"; then
    say "O Claude Code $cc é anterior ao $floor. Atualize com claude update e rode este comando de novo."
    return 1
  fi
  say "Claude Code $cc encontrado."
}

# The last line a failed command printed, without colors or control bytes.
last_line() {
  esc=$(printf '\033')
  sed "s/$esc\[[0-9;]*[A-Za-z]//g" "$1" 2>/dev/null | tr -d '\000-\010\013-\037\177' |
    sed '/^[[:space:]]*$/d' | tail -n 1 | cut -c1-200
}

install_plugin() {
  listed=$(claude plugin list --json </dev/null 2>/dev/null | tr -d ' \t\r\n')
  case $listed in
    *'"id":"unio@unio"'*)
      say "O plugin do Unio já está instalado; a instalação foi pulada."
      entry=${listed#*'"id":"unio@unio"'}
      case ${entry%%[\{\}]*} in
        *'"enabled":false'*) say "O plugin do Unio está desligado neste computador: ligue com claude plugin enable unio@unio." ;;
      esac
      return 0 ;;
  esac
  say "Instalando o plugin do Unio."
  log=$(mktemp 2>/dev/null) || log=/dev/null
  # git asks for credentials on /dev/tty, not stdin, when GitHub refuses it.
  if GIT_TERMINAL_PROMPT=0 claude plugin install unio --marketplace "$marketplace" </dev/null >"$log" 2>&1; then
    [ "$log" = /dev/null ] || rm -f "$log"
    say "Plugin do Unio instalado."
    return 0
  fi
  why=$(last_line "$log")
  [ "$log" = /dev/null ] || rm -f "$log"
  say "Não foi possível instalar o plugin do Unio${why:+ ($why)}. Rode claude plugin install unio --marketplace $marketplace para ver o erro e depois este comando de novo."
  return 1
}

# Claude Code has no command for it, and syncs an autoUpdate declared in
# settings into its marketplace list. So the entry that install --marketplace
# wrote to settings.json gains "autoUpdate": true, but only when it is exactly
# what Claude Code writes; any other shape is the person's and is left alone.
auto_update() {
  cfg=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
  settings=$cfg/settings.json
  if cat "$settings" "$cfg/plugins/known_marketplaces.json" 2>/dev/null | tr -d ' \t\r\n' |
    grep -q -e '"unio":{"source":{[^}]*}[^}]*"autoUpdate":true' -e '"unio":{"autoUpdate":true'; then
    say "A atualização automática do plugin já está ligada."
    return 0
  fi
  if [ -f "$settings" ] && [ ! -L "$settings" ] && [ -w "$settings" ] &&
    awk '
      { line[NR] = $0 }
      END {
        for (i = 1; i <= NR; i++) {
          if (line[i] == "  \"extraKnownMarketplaces\": {") { inside = 1; continue }
          if (inside && line[i] ~ /^  [}]/) inside = 0
          if (inside && line[i] == "    \"unio\": {" &&
            line[i + 1] == "      \"source\": {" &&
            line[i + 2] == "        \"source\": \"github\"," &&
            line[i + 3] == "        \"repo\": \"HL-legaltech/unio-agent\"" &&
            line[i + 4] == "      }" &&
            (line[i + 5] == "    }" || line[i + 5] == "    },")) { found++; at = i + 4 }
        }
        if (found != 1) exit 1
        for (i = 1; i <= NR; i++) {
          if (i == at) { print "      },"; print "      \"autoUpdate\": true" } else print line[i]
        }
      }' "$settings" > "$settings.new" 2>/dev/null &&
    mv -f "$settings.new" "$settings"; then
    say "Atualização automática do plugin ligada, em $(tilde "$settings")."
  else
    rm -f "$settings.new"
    say "Ligue a atualização automática do plugin: no Claude Code, /plugin > Marketplaces > unio > Enable auto-update."
  fi
  return 0
}

install_codex() {
  if ! command -v git >/dev/null 2>&1; then
    say "O comando git não está no PATH. Instale o Git e rode este comando de novo."
    return 1
  fi
  if [ -d "$clone/.git" ] && [ -f "$clone/plugins/unio/skills/unio/SKILL.md" ]; then
    if GIT_TERMINAL_PROMPT=0 git -C "$clone" pull --quiet --ff-only </dev/null >/dev/null 2>&1; then
      say "Unio atualizado em $(tilde "$clone")."
    else
      say "Não foi possível atualizar $(tilde "$clone") com git pull --ff-only; a versão que já está lá continua valendo."
    fi
  elif [ -e "$clone" ] || [ -L "$clone" ]; then
    say "$(tilde "$clone") já existe e não é um clone do Unio. Mova essa pasta e rode este comando de novo."
    return 1
  else
    log=$(mktemp 2>/dev/null) || log=/dev/null
    if ! { mkdir -p "$(dirname -- "$clone")" &&
      GIT_TERMINAL_PROMPT=0 git clone --quiet "https://github.com/$marketplace.git" "$clone" </dev/null >"$log" 2>&1; }; then
      why=$(last_line "$log")
      [ "$log" = /dev/null ] || rm -f "$log"
      say "Não foi possível baixar o Unio em $(tilde "$clone")${why:+ ($why)}. Rode este comando de novo em instantes."
      return 1
    fi
    [ "$log" = /dev/null ] || rm -f "$log"
    say "Unio baixado em $(tilde "$clone")."
  fi

  skill=$clone/plugins/unio/skills/unio
  link=$HOME/.agents/skills/unio
  if [ -L "$link" ] && [ ! -e "$link" ]; then rm -f "$link"; fi
  if [ ! -e "$link" ]; then
    if mkdir -p "$HOME/.agents/skills" && ln -s "$skill" "$link"; then
      say "Skill do Unio ligada em $(tilde "$link")."
    else
      say "Não foi possível criar $(tilde "$link"). Crie um link para $(tilde "$skill") nesse lugar."
    fi
  elif [ "$(CDPATH='' cd -P -- "$link" 2>/dev/null && pwd -P)" = "$(CDPATH='' cd -P -- "$skill" && pwd -P)" ]; then
    say "A skill do Unio já está em $(tilde "$link")."
  else
    say "$(tilde "$link") já existe e não é a skill deste clone. Troque-a por um link para $(tilde "$skill")."
  fi
}

# Codex runs the helper without a shell, so the block names the real home
# folder instead of ~ or $HOME.
codex_config() {
  config=$HOME/.codex/config.toml
  if grep -q 'http_headers_helper.*mcp-headers\.sh' "$config" 2>/dev/null; then
    say "O Codex já lê o token pelo helper do Unio ($(tilde "$config"))."
    return 0
  fi
  case $clone in
    *[\'\"\\]*)
      say "O caminho da sua pasta pessoal tem aspas ou barra invertida: monte o bloco do Codex à mão, como no README (https://github.com/$marketplace#codex)."
      return 0 ;;
  esac
  say "Acrescente este bloco a $(tilde "$config"), no lugar de qualquer [mcp_servers.unio] que já esteja lá:"
  printf '\n[mcp_servers.unio]\nurl = "%s/mcp"\nhttp_headers_helper = "sh '\''%s'\'' codex"\n\n' \
    "$(api)" "$clone/plugins/unio/scripts/mcp-headers.sh"
}

# POSTs the JSON on stdin to path $1 of the API, body to file $2, as lib.sh's
# unio_post does.
post() {
  code=$(curl -sS --max-time 15 -X POST -H 'Content-Type: application/json' \
    --data-binary @- -o "$2" -w '%{http_code}' "$(api)$1" 2>/dev/null)
  case $code in [0-9][0-9][0-9]) ;; *) code=000 ;; esac
  printf '%s' "$code"
}

# The value of key $2 in JSON $1, as lib.sh's unio_json_value reads it.
json_value() {
  printf '%s' "$1" | tr -d '\n' |
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([A-Za-z0-9_:/?=.&-]*\)[\",}].*/\1/p" | head -n 1
}

host_name() {
  h=$(uname -n 2>/dev/null | cut -d. -f1 | tr -cd 'A-Za-z0-9_-' | cut -c1-64)
  case $h in [A-Za-z0-9]*) ;; *) h=computador ;; esac
  printf '%s' "$h"
}

# As lib.sh's unio_open_url: never over SSH or without a display, where
# xdg-open would start a text browser in this terminal.
open_url() {
  [ -z "${SSH_CONNECTION:-}" ] || return 0
  case $(uname -s 2>/dev/null) in
    Darwin)
      if command -v open >/dev/null 2>&1; then (open "$1" </dev/null >/dev/null 2>&1 &); fi ;;
    *)
      if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
        (xdg-open "$1" </dev/null >/dev/null 2>&1 &)
      fi ;;
  esac
  return 0
}

# lib.sh's unio_connect_browser: sets secret, owner, owner_name and days.
connect_browser() {
  secret=
  reply=$(mktemp 2>/dev/null) || reply=
  if [ -z "$reply" ]; then
    say "Não foi possível criar um arquivo temporário. Nada foi salvo."
    return 1
  fi
  trap 'rm -f "$reply"' EXIT
  trap 'rm -f "$reply"; printf "\nCancelado. Nada foi salvo.\n"; exit 130' INT TERM HUP
  status=$(printf '{"client":"%s","host":"%s"}' "$1" "$(host_name)" | post /agents/connect/start "$reply")
  started=$(cat "$reply" 2>/dev/null)
  case $status in
    201) ;;
    000) say "O Unio não respondeu: confira a rede e rode o comando de novo. Nada foi salvo."; return 1 ;;
    404) say "A aprovação pelo navegador ainda não está disponível. Gere um token em Configurações > Pessoal > Agente de código, no Unio, e rode o comando que a página mostra."; return 1 ;;
    429) say "Muitos pedidos de conexão deste endereço. Espere um minuto e rode o comando de novo."; return 1 ;;
    *) say "O Unio respondeu HTTP $status. Nada foi salvo; rode o comando de novo em instantes."; return 1 ;;
  esac
  device=$(json_value "$started" deviceCode)
  user_code=$(json_value "$started" userCode)
  url=$(json_value "$started" verifyUrl)
  interval=$(json_value "$started" interval)
  ttl=$(json_value "$started" expiresIn)
  case $interval in ''|*[!0-9]*) interval=5 ;; esac
  [ "$interval" -ge 1 ] && [ "$interval" -le 60 ] || interval=5
  case $ttl in ''|*[!0-9]*) ttl=600 ;; esac
  case $user_code in [A-Z][A-Z][A-Z][A-Z]-[A-Z][A-Z][A-Z][A-Z]) ;; *) user_code= ;; esac
  case $url in https://?*|http://127.0.0.1:*|http://localhost:*) ;; *) url= ;; esac
  if [ "${#device}" -ne 43 ] || [ -z "$user_code" ] || [ -z "$url" ]; then
    say "O Unio respondeu de um jeito inesperado. Nada foi salvo; rode o comando de novo em instantes."
    return 1
  fi

  say "Abra esta página no navegador, entre no Unio e confira o código $user_code:"
  say ""
  say "  $url"
  say ""
  say "Escolha o workspace na página e aprove. Esperando a aprovação por até $((ttl / 60)) minutos; Ctrl+C cancela."
  open_url "$url"

  deadline=$(($(date +%s) + ttl))
  while :; do
    sleep "$interval"
    if [ "$(date +%s)" -ge "$deadline" ]; then
      say "O pedido venceu sem aprovação. Nada foi salvo; rode o comando de novo."
      return 1
    fi
    status=$(printf '{"deviceCode":"%s"}' "$device" | post /agents/connect/poll "$reply")
    case $status in
      200) break ;;
      202|000) ;;
      429) interval=$((interval + 5)) ;;
      403) say "O pedido foi recusado no navegador. Nada foi salvo."; return 1 ;;
      410) say "O pedido venceu ou já foi usado. Nada foi salvo; rode o comando de novo."; return 1 ;;
      *) say "O Unio respondeu HTTP $status. Nada foi salvo; rode o comando de novo."; return 1 ;;
    esac
  done
  secret=$(sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\(unio_[0-9A-Za-z]*\)".*/\1/p' "$reply" | head -n 1)
  approved=$(sed 's/"token"[[:space:]]*:[[:space:]]*"[^"]*"//' "$reply")
  rm -f "$reply"
  owner=$(json_field "$approved" workspace clerkId)
  owner_name=$(clean "$(json_field "$approved" workspace name)")
  days=$(json_field "$approved" connection expiresInDays)
  if ! is_token "$secret" || ! is_org "$owner"; then
    secret=
    say "O Unio aprovou, mas a resposta veio incompleta. Nada foi salvo; revogue a conexão nova em Configurações > Pessoal > Agente de código e rode o comando de novo."
    return 1
  fi
}

restore_echo() { stty echo </dev/tty 2>/dev/null; }

read_secret() {
  trap restore_echo EXIT
  trap 'restore_echo; exit 130' INT TERM HUP
  # Echo goes off before the prompt shows, so a paste that lands at once is
  # never echoed.
  if ! stty -echo </dev/tty 2>/dev/null; then
    say "Não foi possível esconder a digitação neste terminal. Nada foi lido."
    return 1
  fi
  printf 'Cole o token do Unio: ' >/dev/tty
  IFS= read -r secret </dev/tty || secret=
  restore_echo
  printf '\n' >/dev/tty
  secret=${secret#"${secret%%[![:space:]]*}"}
  secret=${secret%"${secret##*[![:space:]]}"}
}

# The token pasted at a hidden prompt, checked with Unio against
# $workspace; sets secret, name and days.
paste_secret() {
  read_secret || return 1
  if ! is_token "$secret"; then
    say "Isso não é um token do Unio (unio_ seguido de 46 letras e números). Nada foi salvo."
    return 1
  fi

  body=$(mktemp 2>/dev/null) || body=
  if [ -z "$body" ]; then
    say "Não foi possível criar um arquivo temporário. Nada foi salvo."
    return 1
  fi
  code=$(get_me "$secret" "$body")
  json=$(cat "$body" 2>/dev/null)
  rm -f "$body"
  case $code in
    200) ;;
    000) say "O Unio não respondeu: confira a rede e rode o comando de novo. Nada foi salvo."; return 1 ;;
    401) say "O Unio recusou o token. Gere outro na página Agente de código e rode o comando de novo."; return 1 ;;
    403) say "Os agentes do Unio estão desligados neste workspace, então o token não serve agora. Nada foi salvo. Um administrador pode ligá-los em Configurações > Sistema > Agentes."; return 1 ;;
    *) say "O Unio respondeu HTTP $code. Nada foi salvo; rode o comando de novo em instantes."; return 1 ;;
  esac
  owner=$(json_field "$json" workspace clerkId)
  if [ "$owner" != "$workspace" ]; then
    if is_org "$owner"; then
      say "Este token é do workspace $owner, mas o comando é para o $workspace. Nada foi salvo. Use o token e o comando da mesma página Agente de código."
    else
      say "O Unio não confirmou que este token é do workspace $workspace. Nada foi salvo. Use o token e o comando da mesma página Agente de código."
    fi
    return 1
  fi
  name=$(clean "$(json_field "$json" workspace name)")
  days=$(json_field "$json" token expiresInDays)
}

# Whether the computer's own token (~/.config/unio/token, read while no
# default-workspace exists) serves another workspace than $1, or one Unio
# cannot name. A token Unio refuses serves nothing.
legacy_elsewhere() {
  [ -r "$conf/token" ] || return 1
  t=$(tr -d '\r\n' < "$conf/token")
  is_token "$t" || return 1
  body=$(mktemp 2>/dev/null) || return 0
  code=$(get_me "$t" "$body")
  owner=$(json_field "$(cat "$body" 2>/dev/null)" workspace clerkId)
  rm -f "$body"
  case $code in
    401) return 1 ;;
    200) [ "$owner" != "$1" ] ;;
    *) return 0 ;;
  esac
}

ask_scope() {
  {
    say "Este computador já usa o Unio com outro workspace. Onde usar o workspace $workspace?"
    say "  1. Só nesta pasta: $(tilde "$here")"
    say "  2. Em todo o computador, menos nas pastas ligadas a outro workspace"
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
map_folder() {
  tab=$(printf '\t')
  nl='
'
  case $here$logical in *"$tab"*|*"$nl"*) return 1 ;; esac
  {
    if [ -r "$conf/projects" ]; then
      A=$here B=$logical awk -F "$tab" '{
        p = $1; sub(/[[:space:]]+$/, "", p)
        while (p ~ /.\/$/) sub(/\/$/, "", p)
        if (p != ENVIRON["A"] && p != ENVIRON["B"]) print
      }' "$conf/projects"
    fi
    printf '%s\t%s\n' "$here" "$workspace"
    if [ -n "$logical" ] && [ "$logical" != "$here" ]; then printf '%s\t%s\n' "$logical" "$workspace"; fi
  } > "$conf/projects.new" && mv -f "$conf/projects.new" "$conf/projects"
}

# Saves $secret for $workspace (named $1, $2 days left), as lib.sh's
# unio_store_token does, keeping the folders of a --renew.
store_token() {
  here=$(pwd -P)
  logical=$(pwd -L 2>/dev/null)
  default=$(default_org)
  if [ "$(workspace_of "$here")" = "$workspace" ] || [ "$(workspace_of "$logical")" = "$workspace" ]; then
    scope=kept
  elif [ -n "$renew" ] && { [ "$default" = "$workspace" ] || mapped "$workspace"; }; then
    scope=kept
  elif [ -z "$default" ] && ! mapped '' && ! legacy_elsewhere "$workspace"; then
    scope=first
  else
    ask_scope
  fi

  file=$conf/workspaces/$workspace/token
  if ! { mkdir -p "$conf/workspaces/$workspace" &&
    chmod 700 "$conf" "$conf/workspaces" "$conf/workspaces/$workspace" &&
    printf '%s' "$secret" > "$file.new" && mv -f "$file.new" "$file"; } 2>/dev/null; then
    rm -f "$file.new"
    say "Não foi possível gravar $(tilde "$file"). Nada foi salvo."
    return 1
  fi
  printf 'Token salvo em %s' "$(tilde "$file")"
  [ -z "$1" ] || printf ', para o workspace %s' "$1"
  case $2 in ''|*[!0-9]*) printf '.\n' ;; *) printf '. Ele %s.\n' "$(expires_in "$2")" ;; esac

  case $scope in
    first|default)
      if ! { printf '%s\n' "$workspace" > "$conf/default-workspace.new" &&
        mv -f "$conf/default-workspace.new" "$conf/default-workspace"; } 2>/dev/null; then
        rm -f "$conf/default-workspace.new"
        say "Não foi possível gravar $(tilde "$conf/default-workspace"), então nenhuma pasta usa este token ainda."
        return 1
      fi
      if [ "$scope" = first ]; then
        say "O workspace $workspace vale em todas as pastas deste computador."
      else
        say "O workspace $workspace passa a valer em todas as pastas deste computador, menos nas ligadas a outro workspace em $(tilde "$conf/projects")."
      fi
      if [ -z "$default" ] && [ -e "$conf/token" ]; then
        say "O token em $(tilde "$conf/token") deixa de ser lido."
      fi ;;
    folder)
      if ! map_folder 2>/dev/null; then
        rm -f "$conf/projects.new"
        say "Não foi possível ligar esta pasta ao workspace em $(tilde "$conf/projects"), então nenhuma pasta usa este token ainda."
        return 1
      fi
      say "O workspace $workspace vale só em $(tilde "$here") e nas pastas dentro dela." ;;
  esac
}

usage() {
  [ -z "${1:-}" ] || say "$1"
  say "Uso: curl -fsSL https://raw.githubusercontent.com/$marketplace/main/install.sh | sh -s -- [--workspace org_...] [--client claude-code|codex] [--renew] [--browser]"
  exit 2
}

main() {
  # An exported variable of the same name would hand a token to every child.
  unset secret t
  client=claude-code
  workspace=
  renew=
  browser=
  while [ $# -gt 0 ]; do
    case $1 in
      --workspace) [ $# -ge 2 ] || usage "Falta o valor de --workspace."; workspace=$2; shift 2 ;;
      --workspace=*) workspace=${1#*=}; shift ;;
      --client) [ $# -ge 2 ] || usage "Falta o valor de --client."; client=$2; shift 2 ;;
      --client=*) client=${1#*=}; shift ;;
      --renew) renew=1; shift ;;
      --browser) browser=1; shift ;;
      *) usage "Opção desconhecida: $(clean "$1")." ;;
    esac
  done
  if [ -z "$workspace" ]; then
    browser=1
  elif ! is_org "$workspace"; then
    usage "O valor de --workspace não é um workspace do Unio (org_ seguido de letras e números). Copie de novo o comando da página Agente de código."
  fi
  case $client in
    claude-code|codex) ;;
    *) usage "--client aceita claude-code ou codex." ;;
  esac
  if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
    say "A variável HOME não aponta para a sua pasta pessoal. Nada foi instalado."
    return 1
  fi
  if [ -n "${CLAUDECODE:-}" ]; then
    say "Rode este comando em outro terminal, fora do Claude Code: aqui, o que você digita pode entrar na conversa."
    return 1
  fi
  # In a subshell: a failed redirection on a special builtin such as : ends
  # the whole shell under dash.
  if ! ( : </dev/tty ) 2>/dev/null; then
    say "Este comando precisa de um terminal para ler o token sem mostrá-lo. Nada foi instalado."
    return 1
  fi

  if [ -z "$renew" ]; then
    if [ "$client" = codex ]; then
      install_codex || return 1
    else
      need_claude || return 1
      install_plugin || return 1
      auto_update
    fi
  fi

  if [ -n "$browser" ]; then
    connect_browser "$client" || return 1
    say "Aprovado no Unio."
    if [ -n "$workspace" ] && [ "$owner" != "$workspace" ]; then
      say "O pedido foi aprovado no workspace $owner, e não no $workspace do comando; o token vale para o $owner."
    fi
    workspace=$owner
    name=$owner_name
  else
    paste_secret || return 1
  fi
  store_token "$name" "$days" || return 1
  secret=

  if [ "$client" = codex ]; then
    [ -n "$renew" ] || codex_config
    say "Abra uma sessão nova do Codex."
  else
    say "Abra uma sessão nova do Claude Code, ou rode /reload-plugins numa sessão aberta."
  fi
  say "Conectado ao Unio."
}

main "$@"
