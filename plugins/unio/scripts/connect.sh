#!/bin/sh
# Stores a Unio token on this computer without it entering a chat, a command
# line or the shell history.
#
#   connect.sh               The hand setup, in a terminal of your own: reads
#                            the token at a hidden prompt, checks it with Unio,
#                            and only then saves it to ~/.config/unio/token.
#                            With stdin from a pipe (a password manager), reads
#                            it there.
#   connect.sh instructions  What /unio:connect shows inside Claude Code: the
#                            Agente de código page and the installer.

# shellcheck source=lib.sh
. "$(dirname -- "$0")/lib.sh"

# The shortest form of $1 a shell reads back as one word.
quote() {
  case $1 in
    *[!A-Za-z0-9/._-]*) printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf '%s' "$1" ;;
  esac
}


instructions() {
  workspace=$(unio_workspace "${CLAUDE_PROJECT_DIR:-$PWD}")
  file=$(unio_token_file "${CLAUDE_PROJECT_DIR:-$PWD}")
  cat <<EOF
Para conectar este computador ao Unio:

1. No Unio, abra Configurações > Pessoal > Agente de código (https://unio.so/app/settings/coding-agent${workspace:+?ws=$workspace}) e gere um token.
EOF
  if [ -n "$workspace" ]; then
    cat <<EOF
2. Em outro terminal, fora do Claude Code, rode o comando abaixo e cole o token quando ele pedir. Ele troca o token do workspace $workspace, que esta pasta usa. O token não aparece na tela e só é salvo depois que o Unio o aceitar. Para outro workspace, use o comando que a página dele mostra.

$(unio_renew_command "$workspace")

EOF
  else
    cat <<EOF
2. Em outro terminal, fora do Claude Code, rode o comando que a página mostra e cole o token quando ele pedir. O token não aparece na tela e só é salvo depois que o Unio o aceitar.
EOF
  fi
  cat <<EOF
3. De volta aqui, rode /mcp, escolha plugin:unio:unio e depois Reconnect (ou rode /reload-plugins). Em seguida, /unio:doctor confirma a conexão.

Nunca cole o token neste chat.
EOF
  # Only a folder without a workspace reads ~/.config/unio/token, the one
  # file the hand setup writes.
  if [ -z "$workspace" ]; then
    printf '\nNuma configuração à mão, sem o comando da página, este comando faz o mesmo e guarda o token em %s:\n\n%s\n' \
      "$(unio_tilde "$file")" "sh $(quote "$(unio_root)/scripts/connect.sh")"
    [ ! -e "$file" ] || printf '\nJá existe um token em %s; o comando o substitui.\n' "$(unio_tilde "$file")"
  fi
}

read_token() {
  if [ -t 0 ]; then
    trap 'stty echo </dev/tty 2>/dev/null' EXIT
    trap 'stty echo </dev/tty 2>/dev/null; exit 130' INT TERM HUP
    printf 'Cole o token do Unio e tecle Enter (ele não aparece na tela): '
    if ! stty -echo </dev/tty; then
      printf '\nNão foi possível esconder a digitação neste terminal. Nada foi lido.\n'
      exit 1
    fi
    IFS= read -r token </dev/tty || token=
    stty echo </dev/tty
    printf '\n'
  else
    IFS= read -r token || [ -n "$token" ] || token=
  fi
  # A paste often brings spaces or a line break along.
  token=${token#"${token%%[![:space:]]*}"}
  token=${token%"${token##*[![:space:]]}"}
}

connect() {
  if [ -n "${CLAUDECODE:-}" ]; then
    echo "Rode este comando em outro terminal, fora do Claude Code: aqui, o que você digita pode entrar na conversa."
    return 1
  fi
  default=$(unio_workspace '')
  if [ -n "$default" ]; then
    echo "$(unio_tilde "$UNIO_CONF/default-workspace") escolhe o workspace $default para as pastas sem workspace próprio, então nenhuma pasta leria $(unio_tilde "$UNIO_CONF/token"). Nada foi lido. Para trocar o token desse workspace, gere outro em Configurações > Pessoal > Agente de código e rode:"
    echo
    unio_renew_command "$default"
    echo
    return 1
  fi
  if [ -t 0 ] && ! { : </dev/tty; } 2>/dev/null; then
    echo "Este comando precisa de um terminal para ler o token sem mostrá-lo."
    return 1
  fi

  read_token
  if ! unio_is_token "$token"; then
    echo "Isso não é um token do Unio (unio_ seguido de 46 letras e números). Nada foi salvo."
    return 1
  fi

  body=$(umask 077 && mktemp 2>/dev/null) || body=
  if [ -z "$body" ]; then
    echo "Não foi possível criar um arquivo temporário. Nada foi salvo."
    return 1
  fi
  code=$(unio_get_me "$token" "$body")
  json=$(cat "$body" 2>/dev/null)
  rm -f "$body"
  case $code in
    200) ;;
    000)
      echo "O Unio não respondeu: confira a rede e tente de novo. Nada foi salvo."
      return 1 ;;
    401)
      echo "O Unio recusou este token: ele foi revogado, venceu ou é de alguém que perdeu o acesso. Nada foi salvo. Gere outro em Configurações > Pessoal > Agente de código."
      return 1 ;;
    403)
      echo "Os agentes do Unio estão desligados neste workspace, então o token não serve agora. Nada foi salvo. Um administrador pode ligá-los em Configurações > Sistema > Agentes."
      return 1 ;;
    *)
      echo "O Unio respondeu HTTP $code. Nada foi salvo; tente de novo em instantes."
      return 1 ;;
  esac

  # The rename is atomic, so a helper reading the file meanwhile sees the old
  # token or the new one, never half of one.
  if ! (umask 077 && mkdir -p "$UNIO_CONF" &&
    printf '%s' "$token" > "$UNIO_CONF/token.new.$$" &&
    mv -f "$UNIO_CONF/token.new.$$" "$UNIO_CONF/token"); then
    rm -f "$UNIO_CONF/token.new.$$"
    echo "Não foi possível gravar $(unio_tilde "$UNIO_CONF/token"). Nada foi salvo."
    return 1
  fi

  workspace=$(unio_json_field "$json" workspace name | tr -d '\000-\037\177' | cut -c1-80)
  days=$(unio_json_field "$json" token expiresInDays)
  printf 'Token salvo em %s' "$(unio_tilde "$UNIO_CONF/token")"
  [ -z "$workspace" ] || printf ', para o workspace %s' "$workspace"
  case $days in ''|*[!0-9]*) printf '.\n' ;; *) printf '. Ele %s.\n' "$(unio_expires_in "$days")" ;; esac
  echo "No Claude Code, rode /mcp, escolha plugin:unio:unio e depois Reconnect (ou rode /reload-plugins). Em seguida, /unio:doctor confirma a conexão."
}

case ${1:-} in
  instructions) instructions; exit 0 ;;
  '') connect ;;
  *) echo "Uso: connect.sh [instructions]"; exit 2 ;;
esac
