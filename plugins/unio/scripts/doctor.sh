#!/bin/sh
# /unio:doctor's report, one line per check:
#   <ok|warn|bad|info> <area> <check>: <detail>
# core lines decide whether Unio works here; migration and codex lines only
# advise. It never prints a token, and never a name the server returns: this
# output reaches Claude. It always exits 0, because /unio:doctor injects it
# and a failing command aborts the whole command.
#
# Usage: doctor.sh <claude-code|codex>

# shellcheck source=lib.sh
. "$(dirname -- "$0")/lib.sh"

case ${1:-} in codex) kind=codex ;; *) kind=claude-code ;; esac
# pwd -P, never $PWD: macOS's sh keeps a $PWD inherited from whatever
# started it, even when that is another folder.
project=${CLAUDE_PROJECT_DIR:-$(pwd -P)}
root=$(unio_root)
version=$(unio_version "$root")
cfg=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
  claude_json=$CLAUDE_CONFIG_DIR/.claude.json
else
  claude_json=$HOME/.claude.json
fi
settings=$cfg/settings.json

line() { printf '%s %s\n' "$1" "$2"; }

line info "core plugin: unio $version em $(unio_tilde "$root")"
installed=
# unio_root is a physical path, and ~/.claude is often a link (dotfiles).
cfg_physical=$(CDPATH='' cd -P -- "$cfg" 2>/dev/null && pwd -P)
case $root in
  "$cfg"/plugins/cache/*|"${cfg_physical:-$cfg}"/plugins/cache/*) installed=1 ;;
  *) [ "$kind" = codex ] || line info "core plugin: carregado de uma pasta (--plugin-dir), então /unio:update e a atualização automática não se aplicam" ;;
esac

if [ "$kind" = claude-code ]; then
  minimum=$(sed -n 's/^[[:space:]]*"minimumClaudeCode"[[:space:]]*:[[:space:]]*"\([0-9.]*\)".*/\1/p' \
    "$root/.claude-plugin/plugin.json" 2>/dev/null | head -n 1)
  cc=$(claude --version 2>/dev/null | sed -n 's/^\([0-9][0-9.]*\).*/\1/p')
  if [ -z "$cc" ]; then
    line warn "core claude-code: versão desconhecida (o comando claude não está no PATH)"
  elif [ -z "$minimum" ] || unio_version_ge "$cc" "$minimum"; then
    line ok "core claude-code: $cc"
  else
    line bad "core claude-code: $cc é anterior a $minimum; atualize o Claude Code"
  fi
fi

workspace=$(unio_workspace "$project")
file=$(unio_token_file "$project")
if [ -n "$workspace" ] && [ "$workspace" = "$(unio_workspace '')" ]; then
  why="o token do workspace $workspace, o padrão deste computador"
elif [ -n "$workspace" ]; then
  why="o token do workspace $workspace, ligado a esta pasta"
else
  why="o token padrão deste computador"
fi
token=$(unio_read_token "$file")
if [ -z "$token" ]; then
  if [ -e "$file" ]; then
    line bad "core token: $(unio_tilde "$file") ($why) não contém um token do Unio válido; rode /unio:connect"
  else
    line bad "core token: falta $(unio_tilde "$file") ($why); rode /unio:connect"
  fi
else
  # -L judges a linked token by its target, as the helper reads it. Windows
  # guards the home folder with its own permissions, and Git Bash reports
  # modes that chmod cannot change there.
  if [ "$UNIO_OS" = windows ] ||
    [ -z "$(find -L "$file" -prune \( -perm -040 -o -perm -004 -o -perm -020 -o -perm -002 \) 2>/dev/null)" ]; then
    line ok "core token: $(unio_tilde "$file") ($why)"
  else
    line warn "core token: $(unio_tilde "$file") pode ser lido ou alterado por outros usuários; rode chmod 600 nele"
  fi
fi

# The header helper finds this session's folder on its own (unio_agent_dir).
# That matters only where folders are tied to workspaces, and can be checked
# only from inside a session.
if [ "$kind" = claude-code ] && [ -n "${CLAUDECODE:-}" ] && unio_mapped ''; then
  if [ "$UNIO_OS" = windows ]; then
    seen=
    case ${CLAUDE_PID:-} in
      ''|*[!0-9]*) ;;
      *) seen=$(head -n 1 "$(unio_session_file "$CLAUDE_PID")" 2>/dev/null) ;;
    esac
    fix="confira que o Git for Windows está instalado e que os hooks do Claude Code estão ligados, e abra uma sessão nova"
  else
    seen=$(unio_agent_dir)
    fix="o comando ps precisa estar disponível (e o lsof, no macOS)"
  fi
  if [ -z "$seen" ]; then
    line bad "core folder: a conexão com o Unio não consegue saber a pasta desta sessão e por isso conecta sem token, para não usar o workspace de outra pasta; $fix"
  elif [ "$(unio_workspace "$seen")" != "$workspace" ]; then
    line bad "core folder: a conexão com o Unio vê a pasta $(unio_tilde "$seen"), de outro workspace; abra o Claude Code na pasta do projeto"
  else
    line ok "core folder: a conexão com o Unio usa o workspace desta pasta"
  fi
fi

if [ -n "$token" ]; then
  body=$(umask 077 && mktemp 2>/dev/null) || body=
  http=000
  if [ -n "$body" ]; then
    http=$(unio_get_me "$token" "$body" "$kind/$version")
  fi
  json=$(cat "$body" 2>/dev/null)
  [ -z "$body" ] || rm -f "$body"
  case $http in
    200)
      days=$(unio_json_field "$json" token expiresInDays)
      case $days in
        ''|*[!0-9]*) line ok "core api: token aceito" ;;
        *)
          if [ "$days" -le 7 ]; then
            line warn "core api: token aceito, mas $(unio_expires_in "$days"); gere outro em Configurações > Pessoal > Agente de código e rode /unio:connect"
          else
            line ok "core api: token aceito; $(unio_expires_in "$days")"
          fi ;;
      esac
      status=$(unio_json_field "$json" skill status)
      current=$(unio_json_field "$json" skill current)
      minimum=$(unio_json_field "$json" skill minimum)
      unio_is_version "$current" || current='?'
      unio_is_version "$minimum" || minimum='?'
      case $status in
        current) line ok "core version: $version é a versão atual" ;;
        behind) line warn "core version: $version; a mais nova é $current; rode /unio:update" ;;
        unsupported) line bad "core version: $version está abaixo do mínimo $minimum, e as ferramentas do Unio recusam chamadas; rode /unio:update" ;;
        *) line info "core version: o servidor não avaliou a versão $version" ;;
      esac ;;
    401) line bad "core api: o Unio recusou o token (revogado, vencido, ou de quem perdeu o acesso); gere outro em Configurações > Pessoal > Agente de código e rode /unio:connect" ;;
    403) line bad "core api: os agentes estão desligados neste workspace; um administrador os liga em Configurações > Sistema > Agentes" ;;
    000) line bad "core api: sem resposta de $(unio_api)" ;;
    *) line bad "core api: HTTP $http de $(unio_api)/agents/me" ;;
  esac
fi

if [ "$kind" = claude-code ] && [ -n "$installed" ]; then
  if cat "$settings" "$cfg/plugins/known_marketplaces.json" 2>/dev/null | tr -d ' \t\r\n' |
    grep -q -e '"unio":{"source":{[^}]*}[^}]*"autoUpdate":true' -e '"unio":{"autoUpdate":true'; then
    line ok "core updates: atualização automática ligada para o marketplace unio"
  else
    line warn "core updates: atualização automática parece desligada; ligue em /plugin > Marketplaces > unio > Enable auto-update"
  fi
fi

if [ "$kind" = claude-code ]; then
  [ -e "$cfg/skills/unio" ] &&
    line warn "migration user-skill: $(unio_tilde "$cfg/skills/unio") também carrega uma skill unio; mova a pasta para fora de $(unio_tilde "$cfg/skills")"
  if grep -q 'api\.unio\.so/v1/mcp' "$claude_json" 2>/dev/null; then
    line warn "migration user-mcp: $(unio_tilde "$claude_json") declara o servidor do Unio e ele tem precedência sobre o do plugin; salve a entrada e rode claude mcp remove unio -s user"
  fi
  if grep -q '"Bearer unio_' "$claude_json" 2>/dev/null; then
    line warn "migration static-token: $(unio_tilde "$claude_json") guarda um token do Unio em texto (um cabeçalho Authorization fixo); remova essa entrada depois de conectar com /unio:connect"
  fi
  if grep -q 'mcp__unio__' "$settings" 2>/dev/null; then
    grep -q -e 'mcp__plugin_unio_unio__' -e 'mcp__(plugin_unio_)?unio__' "$settings" ||
      line warn "migration settings: $(unio_tilde "$settings") cita mcp__unio__ (permissões ou matcher de hook); as ferramentas do plugin se chamam mcp__plugin_unio_unio__*"
  fi
fi

if [ -d "$HOME/.codex" ]; then
  if [ -e "$HOME/.agents/skills/unio/SKILL.md" ]; then
    line ok "codex skill: ~/.agents/skills/unio"
  else
    line info "codex skill: não instalada (veja a seção Codex do README)"
  fi
  clone=$HOME/.local/share/unio/agent/plugins/unio
  [ -d "$clone" ] && line info "codex clone: unio $(unio_version "$clone") em $(unio_tilde "$clone")"
  codex_config=$HOME/.codex/config.toml
  if grep -q 'http_headers_helper.*mcp-headers\.sh' "$codex_config" 2>/dev/null; then
    line ok "codex config: o servidor unio lê o token pelo helper"
  elif grep -q '^\[mcp_servers\.unio\]' "$codex_config" 2>/dev/null; then
    line warn "codex config: [mcp_servers.unio] não usa o helper; um token exportado em variável chega a todo programa que o shell inicia"
  fi
fi
exit 0
