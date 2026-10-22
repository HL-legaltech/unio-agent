#!/bin/sh
# Stores a Unio token on this computer without it entering a chat, a command
# line or the shell history.
#
#   connect.sh               In a terminal of your own: reads the token at a
#                            hidden prompt, checks it with Unio, and only then
#                            saves it to ~/.config/unio/token. With stdin from
#                            a pipe (a password manager), reads it there.
#   connect.sh instructions  What /unio:connect shows inside Claude Code.

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
  command="sh $(quote "$(unio_root)/scripts/connect.sh")"
  file=$(unio_token_file "${CLAUDE_PROJECT_DIR:-$PWD}")
  default=$(unio_workspace '')
  cat <<EOF
Para conectar este computador ao Unio:

1. Gere um token no Unio, em Configurações > Sistema > Agentes > Gerar token (https://unio.so/app/settings/agents). Essa página aparece para administradores do workspace com papel de desenvolvedor; se não for o seu caso, peça o token a um deles.
2. Em outro terminal, fora do Claude Code, rode o comando abaixo e cole o token quando ele pedir. O token não aparece na tela e só é salvo depois que o Unio o aceitar.

$command

3. De volta aqui, rode /mcp, escolha plugin:unio:unio e depois Reconnect (ou rode /reload-plugins). Em seguida, /unio:doctor confirma a conexão.

Nunca cole o token neste chat.
EOF
  if [ -n "$default" ]; then
    printf '\nAtenção: %s escolhe o workspace %s para as pastas sem workspace próprio, então nenhuma pasta lê %s e o comando se recusa a gravá-lo. Apague esse arquivo antes de rodar o comando, ou troque o token desta pasta direto em %s.\n' \
      "$(unio_tilde "$UNIO_CONF/default-workspace")" "$default" "$(unio_tilde "$UNIO_CONF/token")" "$(unio_tilde "$file")"
  elif [ "$file" != "$UNIO_CONF/token" ]; then
    printf '\nEsta pasta usa o token de outro workspace (%s). O comando grava %s, que vale só para as pastas fora de %s.\n' \
      "$(unio_tilde "$file")" "$(unio_tilde "$UNIO_CONF/token")" "$(unio_tilde "$UNIO_CONF/projects")"
  elif [ -e "$file" ]; then
    printf '\nJá existe um token em %s; o comando o substitui.\n' "$(unio_tilde "$file")"
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
    echo "$(unio_tilde "$UNIO_CONF/default-workspace") escolhe o workspace $default para as pastas sem workspace próprio, então nenhuma pasta leria $(unio_tilde "$UNIO_CONF/token"). Apague esse arquivo e rode este comando de novo. Nada foi lido."
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
      echo "O Unio recusou este token: ele foi revogado, venceu ou é de alguém que perdeu o acesso. Nada foi salvo. Gere outro em Configurações > Sistema > Agentes."
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
