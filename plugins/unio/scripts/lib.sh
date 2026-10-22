# Sourced by the plugin's scripts. POSIX sh and utilities, plus curl, mktemp
# and date +%s: they run under dash, bash, macOS sh and Git Bash.

# Byte semantics for every pattern and length below; output is unaffected.
LC_ALL=C
export LC_ALL

UNIO_CONF=$HOME/.config/unio

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

# The workspace a folder works in: the longest folder in
# ~/.config/unio/projects ("<folder><TAB><org id>" per line) that holds it,
# else the one in ~/.config/unio/default-workspace. Prints nothing when
# neither applies, which means the single token in ~/.config/unio/token.
unio_workspace() {
  org=
  if [ -n "$1" ] && [ -r "$UNIO_CONF/projects" ]; then
    best=
    tab=$(printf '\t')
    while IFS=$tab read -r path key || [ -n "$path" ]; do
      # A line saved on Windows ends in CR, which would fail unio_is_org and
      # drop the mapping without a word, sending another workspace's token.
      key=${key#"${key%%[![:space:]]*}"}
      key=${key%"${key##*[![:space:]]}"}
      path=${path%"${path##*[![:space:]]}"}
      while case $path in ?*/) true ;; *) false ;; esac; do path=${path%/}; done
      if [ -z "$path" ] || ! unio_is_org "$key"; then continue; fi
      case $1/ in
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
