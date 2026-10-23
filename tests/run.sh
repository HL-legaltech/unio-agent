#!/bin/sh
# Runs the plugin's scripts and install.sh against tests/fake_api.py in a
# throwaway HOME: no real token, config or API is touched, and nothing leaves
# the machine. The scripts run under the shell that runs this file, so CI
# runs it twice:
#   dash tests/run.sh
#   bash tests/run.sh
set -u

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
plugin=$(CDPATH='' cd -- "$here/../plugins/unio" && pwd -P)
scripts=$plugin/scripts
shell=$(ps -o comm= -p $$ 2>/dev/null | sed 's|.*/||')
case $shell in dash|bash|sh|ksh|mksh|zsh) ;; *) shell="sh" ;; esac
version=$(sed -n 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$plugin/.claude-plugin/plugin.json")

# Fake tokens in the real shape, unio_ and 46 base62 characters, so the
# scripts treat them as tokens and the fake API tells them apart.
fake_token() { printf 'unio_%.46s' "$1$1$1$1"; }
good=$(fake_token FakeTokenNotASecret)
other=$(fake_token OtherFakeTokenNotASecret)
refused=$(fake_token RevokedFakeTokenNotASecret)
off=$(fake_token AgentsOffFakeTokenNotASecret)
second=$(fake_token SecondWorkspaceFakeTokenNotASecret)

tmp=$(mktemp -d "${TMPDIR:-/tmp}/unio-tests.XXXXXX")
server=
trap 'if [ -n "$server" ]; then kill "$server" 2>/dev/null; fi; rm -rf "$tmp"' EXIT
trap 'exit 130' INT TERM HUP

unset CLAUDECODE CLAUDE_CONFIG_DIR CLAUDE_PROJECT_DIR UNIO_INBOX_RUN XDG_STATE_HOME
# No browser opens unless a check asks for one, and then only the fake below.
unset DISPLAY WAYLAND_DISPLAY SSH_CONNECTION
HOME=$tmp/home
CLAUDE_PLUGIN_DATA=$tmp/data
export HOME CLAUDE_PLUGIN_DATA
conf=$HOME/.config/unio
status=$CLAUDE_PLUGIN_DATA/status-default
mkdir -p "$conf" "$tmp/bin" "$tmp/elsewhere"

# Every curl argv is logged, so a check can prove no token was ever on one.
real_curl=$(command -v curl)
cat > "$tmp/bin/curl" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$tmp/curl-argv.log"
exec "$real_curl" "\$@"
EOF
# The claude CLI, as far as the doctor and the installer call it. Its
# arguments are logged too. install --marketplace writes settings.json the
# way Claude Code 2.1.308 does, byte for byte.
cat > "$tmp/bin/claude" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$tmp/claude-argv.log"
cfg=\${CLAUDE_CONFIG_DIR:-\$HOME/.claude}
case "\$*" in
  --version) printf '%s (Claude Code)\n' "\$(cat "$tmp/claude-version" 2>/dev/null || echo 2.1.308)" ;;
  "plugin list --json")
    if [ -e "\$cfg/fake-installed" ]; then
      enabled=true
      [ ! -e "\$cfg/fake-disabled" ] || enabled=false
      printf '[\n  {\n    "id": "unio@unio",\n    "version": "1.0.0",\n    "scope": "user",\n    "enabled": %s,\n    "mcpServers": {\n      "unio": {\n        "type": "http"\n      }\n    }\n  }\n]\n' "\$enabled"
    else
      echo '[]'
    fi ;;
  "plugin install unio --marketplace HL-legaltech/unio-agent")
    if [ -e "$tmp/claude-fails" ]; then echo "Failed to add marketplace: network unreachable" >&2; exit 1; fi
    mkdir -p "\$cfg"
    [ -e "\$cfg/settings.json" ] || cat > "\$cfg/settings.json" <<'JSON'
{
  "extraKnownMarketplaces": {
    "unio": {
      "source": {
        "source": "github",
        "repo": "HL-legaltech/unio-agent"
      }
    }
  },
  "enabledPlugins": {
    "unio@unio": true
  }
}
JSON
    touch "\$cfg/fake-installed"
    echo "Successfully installed plugin: unio@unio (scope: user)" ;;
  *) echo "unexpected claude call: \$*" >&2; exit 64 ;;
esac
EOF
# git, for the installer's clone: a clone copies this plugin and bin/, and
# the stub runner/ once a check made one. It fails while $tmp/git-fails exists.
mkdir -p "$tmp/git-bin"
cat > "$tmp/git-bin/git" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$tmp/git-argv.log"
[ ! -e "$tmp/git-fails" ] || { echo "fatal: unable to access GitHub" >&2; exit 128; }
case \$1 in
  clone) for a; do dest=\$a; done; mkdir -p "\$dest/.git" "\$dest/plugins" && cp -R "$plugin" "\$dest/plugins/unio" &&
    cp -R "$here/../bin" "\$dest/bin" &&
    if [ -d "$tmp/runner-stub" ]; then cp -R "$tmp/runner-stub" "\$dest/runner"; fi ;;
  -C) [ "\$3" = pull ] ;;
  *) exit 64 ;;
esac
EOF
# xdg-open, as far as the browser approval calls it: it logs the URL.
cat > "$tmp/bin/xdg-open" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$tmp/xdg-open.log"
EOF
chmod +x "$tmp/bin/curl" "$tmp/bin/claude" "$tmp/git-bin/git" "$tmp/bin/xdg-open"
PATH=$tmp/bin:$PATH

free_port() {
  python3 -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}
port=$(free_port)
closed=$(free_port)
cp "$here/fixtures/skill_block.json" "$tmp/skill_block.json"
mkdir -p "$tmp/connect"
FAKE_CONNECT_DIR=$tmp/connect python3 -I "$here/fake_api.py" "$port" "$tmp/clients.log" "$tmp/skill_block.json" \
  "$refused" "$off" "$good" "$other" "$second=org_Second" 2>"$tmp/server.err" &
server=$!
tries=0
until "$real_curl" -s -o /dev/null "http://127.0.0.1:$port/"; do
  tries=$((tries + 1))
  if [ "$tries" -ge 60 ]; then echo "The fake API did not start:"; cat "$tmp/server.err"; exit 1; fi
  sleep 1
done
api() { printf 'http://127.0.0.1:%s\n' "$1" > "$conf/api-url"; }
api "$port"

# Each check follows the condition it names: "condition; check name".
fails=0
checks=0
check() {
  result=$?
  checks=$((checks + 1))
  if [ "$result" -eq 0 ]; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi
}
has() { printf '%s' "$1" | grep -q -e "$2"; }
lacks() { ! printf '%s' "$1" | grep -q -e "$2"; }
has_line() { printf '%s\n' "$1" | grep -qxF -e "$2"; }
is_json() { printf '%s' "$1" | python3 -I -m json.tool >/dev/null 2>&1; }
private() { [ -n "$(find "$1" -prune -perm 600 2>/dev/null)" ]; }
calls() { wc -l < "$tmp/clients.log" 2>/dev/null | tr -d ' '; }
last_client() { tail -n 1 "$tmp/clients.log"; }
# A field of the hook's JSON output, or nothing.
hook() {
  printf '%s' "$1" | python3 -I -c '
import json, sys
text = sys.stdin.read().strip()
data = json.loads(text) if text else {}
value = data.get("systemMessage", "") if sys.argv[1] == "user" else data.get("hookSpecificOutput", {}).get("additionalContext", "")
print(value)' "$2"
}
# Runs a command as a coding agent runs it: from a process that is not a
# shell (python here, Claude Code or Codex for real), working in folder $1,
# with the command itself working in folder $2.
agent() {
  python3 -I -c 'import os, subprocess, sys
os.chdir(sys.argv[1])
sys.exit(subprocess.call(sys.argv[3:], cwd=sys.argv[2]))' "$@"
}
# The header helper as Claude Code starts it: in the plugin's folder, with no
# CLAUDE_PROJECT_DIR, for an agent working in folder $1.
headers() { agent "$1" "$plugin" "$shell" "$scripts/mcp-headers.sh" "${2:-claude-code}"; }
notice() { "$shell" "$scripts/session-start.sh" notice; }
refresh() { "$shell" "$scripts/session-start.sh" refresh; }
connect() { printf '%s\n' "$1" | "$shell" "$scripts/connect.sh"; }
doctor() { "$shell" "$scripts/doctor.sh" claude-code; }
serve() { printf '{"skill":%s}\n' "$1" > "$tmp/skill_block.json"; }
# Serves the fixture with the token's days left set to $1.
days_left() { sed "s/}\$/,\"expiresInDays\":$1}/" "$here/fixtures/skill_block.json" > "$tmp/skill_block.json"; }
# Ages a status file, so the next refresh asks the API again.
expire() { touch -t 202001010000 "$CLAUDE_PLUGIN_DATA/$1"; }

echo "Shell: $shell; plugin $version; fake API on port $port"

python3 -I "$here/check_fixture.py"; check "the contract fixture is a flat skill block"
served=$(printf 'header = "Authorization: Bearer %s"\n' "$good" | "$real_curl" -s -K - "http://127.0.0.1:$port/v1/agents/me")
fixture=$(tr -d '\n' < "$here/fixtures/skill_block.json")
fixture=${fixture#?}
printf '%s' "$served" | grep -qF -e "${fixture%?}" && lacks "$served" '": '; check "the fake API serves the fixture's text, compact as the backend's JSON"
out=$("$shell" "$here/check_versions.sh" refs/heads/no-such-base 2>&1)
code=$?
[ "$code" -eq 1 ] && has "$out" "no-such-base does not exist" && lacks "$out" "Versions agree"; check "the version gate fails on a base ref that does not exist"
shaped=$(printf '%s\n' "$good" "$other" "$refused" "$off" "$second" | grep -cx 'unio_[0-9A-Za-z]\{46\}')
[ "$shaped" -eq 5 ]; check "the fake tokens have the real shape"

# The command skills. In the default permission mode, a !-injected command
# runs only when an allowed-tools Bash rule matches it exactly.
injected=$(for f in "$plugin"/skills/*/SKILL.md; do
  awk '
    NR == 1 && /^---$/ { front = 1; next }
    front && /^---$/ { front = 0; next }
    front {
      while (match($0, /Bash\([^)]*\)/)) {
        allowed[substr($0, RSTART + 5, RLENGTH - 6)] = 1
        $0 = substr($0, RSTART + RLENGTH)
      }
      next
    }
    /^!`.*`$/ {
      command = substr($0, 3, length($0) - 3)
      print ((command in allowed) ? "allowed " : "unallowed ") FILENAME ": " command
    }
  ' "$f"
done)
has "$injected" "^allowed .*/connect/SKILL.md" && has "$injected" "^allowed .*/doctor/SKILL.md" &&
  lacks "$injected" "^unallowed"; check "every injected command has an exact allowed-tools rule"

# The header helper.
out=$(headers "$tmp/elsewhere")
[ "$out" = "{\"X-Unio-Client\":\"claude-code/$version\"}" ]; check "headers without a token send only the client"
is_json "$out"; check "headers are JSON"
out=$(headers "$tmp/elsewhere" codex)
[ "$out" = "{\"X-Unio-Client\":\"codex/$version\"}" ]; check "Codex sends its own client kind"
printf 'unio_short\n' > "$conf/token"
out=$(headers "$tmp/elsewhere")
lacks "$out" Authorization; check "a malformed token is never sent"
printf '%s\r\n' "$good" > "$conf/token"
out=$(headers "$tmp/elsewhere")
[ "$out" = "{\"Authorization\":\"Bearer $good\",\"X-Unio-Client\":\"claude-code/$version\"}" ]; check "headers send the token and the client"
is_json "$out"; check "headers with a token are JSON"
out=$(UNIO_INBOX_RUN=1 headers "$tmp/elsewhere")
lacks "$out" Authorization && has "$out" X-Unio-Client; check "an inbox run gets no personal token"

work=$tmp/work
mkdir -p "$work/a/sub" "$work/ab" "$work/b" "$conf/workspaces/org_A"
printf '%s' "$other" > "$conf/workspaces/org_A/token"
printf '%s\torg_A\n%s\torg_C\n%s/\torg_B/../x\n' "$work/a" "$work" "$work/b" > "$conf/projects"
out=$(headers "$work/a/sub")
has "$out" "Bearer $other"; check "a folder mapped to a workspace sends that workspace's token"
out=$(CLAUDE_PROJECT_DIR=$work/a headers "$tmp/elsewhere")
has "$out" "Bearer $other"; check "CLAUDE_PROJECT_DIR picks the folder before the working directory"
out=$(headers "$work/ab")
lacks "$out" Authorization; check "a sibling folder with the same prefix does not match"
out=$(headers "$work/b")
lacks "$out" Authorization; check "the longest match wins, and its missing token is never replaced by another"
printf '%s/ \torg_A \r\n' "$work/a" > "$conf/projects"
out=$(headers "$work/a/sub")
has "$out" "Bearer $other"; check "a projects line saved on Windows, with stray spaces, still maps its folder"
printf 'org_A\n' > "$conf/default-workspace"
out=$(headers "$tmp/elsewhere")
has "$out" "Bearer $other"; check "default-workspace applies outside mapped folders"

# Without ps, the helper cannot read the agent's folder.
mkdir -p "$tmp/no-ps"
printf '#!/bin/sh\nexit 1\n' > "$tmp/no-ps/ps"
chmod +x "$tmp/no-ps/ps"
printf '%s\torg_A\n' "$work/a" > "$conf/projects"
mkdir -p "$conf/workspaces/org_B"
printf '%s' "$good" > "$conf/workspaces/org_B/token"
printf 'org_B\n' > "$conf/default-workspace"
out=$(PATH=$tmp/no-ps:$PATH headers "$work/a")
lacks "$out" Authorization && has "$out" X-Unio-Client; check "an unknown folder on a computer with mapped folders sends no token, never the default's"
rm -f "$conf/projects"
out=$(PATH=$tmp/no-ps:$PATH headers "$work/a")
has "$out" "Bearer $good"; check "an unknown folder on a computer without mapped folders sends the default's token"

# Folder forms: Claude Code says C:/x on Windows where Git Bash's pwd -P,
# which the installer writes, says /c/x; Windows and macOS ignore case.
# shellcheck disable=SC2016 # the script's own expansions, in its shell
forms=$("$shell" -c '
  . "$1"
  printf "org_B\n" > "$UNIO_CONF/default-workspace"
  UNIO_OS=windows
  printf "%s|" "$(unio_dir_key "C:\\Users\\Ana\\Repo\\")" "$(unio_dir_key c:/Users/ana/repo)" "$(unio_dir_key /c/Users/Ana/Repo)"
  printf "/c/Users/Ana/Repo\torg_A\n" > "$UNIO_CONF/projects"
  printf "%s|" "$(unio_workspace C:/Users/ANA/repo/sub)" "$(unio_workspace "C:\\Users\\Ana\\Repository")"
  UNIO_OS=macos
  printf "/Users/Ana/Repo\torg_A\n" > "$UNIO_CONF/projects"
  printf "%s|" "$(unio_workspace /Users/ana/REPO)"
  UNIO_OS=linux
  printf "%s|" "$(unio_workspace /Users/ana/REPO)"
' sh "$scripts/lib.sh")
[ "$forms" = "/c/users/ana/repo|/c/users/ana/repo|/c/users/ana/repo|org_A|org_B|org_A|org_B|" ]; check "Windows folder forms and case-insensitive systems map one folder (got $forms)"
mkdir -p "$tmp/real-repo/sub"
ln -s "$tmp/real-repo" "$tmp/linked-repo"
printf '%s\torg_A\n' "$tmp/real-repo" > "$conf/projects"
# shellcheck disable=SC2016 # the script's own expansions, in its shell
linked=$("$shell" -c '. "$1"; unio_workspace "$2"' sh "$scripts/lib.sh" "$tmp/linked-repo/sub")
[ "$linked" = org_A ]; check "a folder reached through a link maps as the folder it points to"
rm -f "$conf/default-workspace" "$conf/projects" "$conf/token"
rm -rf "$conf/workspaces"

# The connect script.
out=$("$shell" "$scripts/connect.sh" instructions)
has "$out" "Configurações > Pessoal > Agente de código (https://unio.so/app/settings/coding-agent)" &&
  has "$out" "rode o comando que a página mostra"; check "connect sends a computer without a workspace to the Agente de código page"
has_line "$out" "sh $scripts/connect.sh" && has "$out" "Nunca cole o token neste chat"; check "connect prints the command to run elsewhere"
has_line "$out" "sh $scripts/connect.sh browser" && has "$out" "confira que o código é o mesmo do terminal"; check "connect leads with the browser approval"
mkdir -p "$tmp/my plugins"
cp -R "$plugin" "$tmp/my plugins/unio"
out=$("$shell" "$tmp/my plugins/unio/scripts/connect.sh" instructions)
has_line "$out" "sh '$tmp/my plugins/unio/scripts/connect.sh'"; check "connect quotes a path with a space"
out=$(printf '%s\n' "$good" | CLAUDECODE=1 "$shell" "$scripts/connect.sh")
has "$out" "fora do Claude Code" && [ ! -e "$conf/token" ]; check "connect refuses to read a token inside Claude Code"
out=$(connect unio_short)
has "$out" "não é um token do Unio" && [ ! -e "$conf/token" ]; check "connect refuses a malformed token and saves nothing"
before=$(calls)
out=$(connect "${good}x")
[ "$(calls)" -eq "$before" ] && [ ! -e "$conf/token" ]; check "connect never sends a malformed token to Unio"
api "$closed"
out=$(connect "$good")
has "$out" "não respondeu" && [ ! -e "$conf/token" ]; check "connect tells a network failure apart and saves nothing"
api "$port"
out=$(connect "$refused")
has "$out" "recusou este token" && [ ! -e "$conf/token" ]; check "connect tells a refused token apart and saves nothing"
out=$(connect "$off")
has "$out" "desligados" && [ ! -e "$conf/token" ]; check "connect reports agents switched off and saves nothing"
mkdir -p "$tmp/mapped"
printf '%s\torg_A\n' "$tmp/mapped" > "$conf/projects"
out=$(CLAUDE_PROJECT_DIR=$tmp/mapped "$shell" "$scripts/connect.sh" instructions)
renew_a="curl -fsSL https://raw.githubusercontent.com/HL-legaltech/unio-agent/main/install.sh | sh -s -- --workspace org_A --renew"
has_line "$out" "$renew_a" && has "$out" "coding-agent?ws=org_A" && ! has_line "$out" "sh $scripts/connect.sh" &&
  has_line "$out" "sh $scripts/connect.sh browser"; check "connect gives a folder with a workspace the browser approval and the installer's renewal, and no hand command"
printf 'org_A\n' > "$conf/default-workspace"
out=$("$shell" "$scripts/connect.sh" instructions)
has_line "$out" "$renew_a" && ! has_line "$out" "sh $scripts/connect.sh"; check "connect gives the default workspace's renewal, and no hand command, while default-workspace exists"
before=$(calls)
out=$(connect "$good")
has "$out" "default-workspace escolhe o workspace org_A" && has_line "$out" "$renew_a" && [ ! -e "$conf/token" ] &&
  [ "$(calls)" -eq "$before" ]; check "connect saves no token that no folder would read, and gives the installer's renewal"
rm -f "$conf/default-workspace" "$conf/projects"
out=$(connect "  $good  ")
has "$out" "Token salvo" && [ "$(cat "$conf/token")" = "$good" ]; check "connect saves an accepted token"
private "$conf/token"; check "the saved token is private"
lacks "$out" "unio_"; check "connect never prints the token"
[ -z "$(find "$conf" -name 'token.new.*')" ]; check "connect leaves no temporary token file"

# The session hook. The token saved above is the computer's token from here on.
mv "$conf/token" "$tmp/saved-token"
out=$(notice)
is_json "$out"; check "the notice is JSON"
user=$(hook "$out" user)
model=$(hook "$out" model)
has "$user" "não está conectado" && has "$user" "/unio:connect" && has "$user" "Nunca cole o token"; check "not connected: the person is told to connect and never to paste the token"
has "$model" "Never ask them to paste a token"; check "not connected: Claude is told never to ask for the token"
printf 'garbage\n' > "$conf/token"
user=$(hook "$(notice)" user)
has "$user" "não contém um token do Unio válido"; check "a token file holding no token is named"
refresh
[ ! -e "$status" ]; check "refresh without a token keeps no status"
mv "$tmp/saved-token" "$conf/token"

before=$(calls)
refresh
[ "$(calls)" -eq $((before + 1)) ] && [ "$(last_client)" = "claude-code/$version" ]; check "refresh asks the API with the client header"
! grep -v -e '^checked=[0-9]*$' -e '^version=[0-9.]*$' -e '^http=[0-9][0-9][0-9]$' -e '^status=[a-z]*$' \
  -e '^current=[0-9.]*$' -e '^minimum=[0-9.]*$' -e '^days=[0-9]*$' "$status"; check "refresh keeps only codes, numbers and versions"
grep -qx http=200 "$status" && grep -qx status=behind "$status" && grep -qx current=1.1.0 "$status" &&
  grep -qx minimum=1.0.0 "$status" && grep -qx days=5 "$status"; check "refresh reads the fixture's skill block"
! grep -q -e Marker -e Laptop "$status"; check "refresh never stores a name"
private "$status"; check "the status file is private"
refresh
[ "$(calls)" -eq $((before + 1)) ]; check "refresh asks at most every 6 hours"
# The fixture's release is no newer than this plugin from 1.1.0 on, so the
# notice is served the next major release.
next=$((${version%%.*} + 1))
serve "{\"current\":\"$next.0.0\",\"minimum\":\"1.0.0\",\"yours\":\"$version\",\"status\":\"behind\",\"updateUrl\":\"https://github.com/HL-legaltech/unio-agent#update\"}"
expire status-default
refresh
before=$(calls)

out=$(notice)
[ "$(calls)" -eq "$before" ]; check "the notice reads local files only"
user=$(hook "$out" user)
model=$(hook "$out" model)
is_json "$out"; check "the notice is JSON after a check"
has "$user" "O token do Unio deste computador vence em 5 dias"; check "the notice warns the token expires within 7 days, naming the computer's token"
has "$user" "Saiu a versão $next.0.0 " && has "$user" "/unio:update"; check "the notice announces a newer release"
[ -z "$model" ]; check "update news is for the person only"
user=$(hook "$(notice)" user)
lacks "$user" "Saiu a versão"; check "a release is announced once"

printf '0.9.0' > "$CLAUDE_PLUGIN_DATA/seen-version"
out=$(notice)
user=$(hook "$out" user)
model=$(hook "$out" model)
first=$(awk '/^## /{n++; next} n==1 && NF { print; exit }' "$plugin/CHANGELOG.md")
has "$user" "atualizado para $version" && has_line "$user" "$first"; check "an update shows the version and the CHANGELOG's first section"
[ -z "$model" ]; check "CHANGELOG text never reaches Claude"
user=$(hook "$(notice)" user)
lacks "$user" "atualizado para"; check "the update notice shows once per version"

serve "{\"current\":\"$next.1\",\"minimum\":\"1.0\",\"yours\":\"$version\",\"status\":\"behind\",\"updateUrl\":\"https://github.com/HL-legaltech/unio-agent#update\"}"
expire status-default
refresh
user=$(hook "$(notice)" user)
grep -qx "current=$next.1" "$status" && grep -qx minimum=1.0 "$status" && has "$user" "Saiu a versão $next.1 "; check "two-part versions from the server are read"
serve '{"current":"1.2.0","minimum":"1.2","yours":"1.0.0","status":"unsupported","updateUrl":"https://github.com/HL-legaltech/unio-agent#update"}'
expire status-default
refresh
out=$(notice)
user=$(hook "$out" user)
model=$(hook "$out" model)
has "$user" "abaixo da versão mínima que o Unio aceita (1.2)" && has "$model" "below the minimum"; check "below the minimum, the person and Claude are told to update"
cp "$tmp/my plugins/unio/.claude-plugin/plugin.json" "$tmp/plugin.json"
# A release above this one and above the served minimum, whatever this is.
later=$((${version%%.*} + 1)).0.0
sed "s/\"version\": *\"[^\"]*\"/\"version\": \"$later\"/" "$tmp/plugin.json" > "$tmp/my plugins/unio/.claude-plugin/plugin.json"
user=$(hook "$("$shell" "$tmp/my plugins/unio/scripts/session-start.sh" notice)" user)
has "$user" "atualizado para $later" && lacks "$user" "abaixo da versão mínima"; check "after an update, the old version's verdict is not repeated"
printf '%s' "$version" > "$CLAUDE_PLUGIN_DATA/seen-version"
cp "$here/fixtures/skill_block.json" "$tmp/skill_block.json"
expire status-default
refresh

mkdir -p "$work/r" "$work/o" "$conf/workspaces/org_R" "$conf/workspaces/org_O"
printf '%s' "$refused" > "$conf/workspaces/org_R/token"
printf '%s' "$off" > "$conf/workspaces/org_O/token"
printf '%s\torg_R\n%s\torg_O\n' "$work/r" "$work/o" > "$conf/projects"
CLAUDE_PROJECT_DIR=$work/r refresh
out=$(CLAUDE_PROJECT_DIR=$work/r notice)
user=$(hook "$out" user)
model=$(hook "$out" model)
grep -qx http=401 "$CLAUDE_PLUGIN_DATA/status-org_R" && grep -qx http=200 "$status"; check "each workspace keeps its own status"
has "$user" "recusou o token do workspace org_R" && has "$model" "refused"; check "a refused token is reported to the person, naming its workspace, and to Claude"
lacks "$model" "org_R"; check "Claude's context gets fixed text, without the workspace"
printf '%s' "$good" > "$conf/workspaces/org_R/token"
expire status-org_R
user=$(hook "$(CLAUDE_PROJECT_DIR=$work/r notice)" user)
lacks "$user" "recusou"; check "a new token silences the old refusal"
CLAUDE_PROJECT_DIR=$work/o refresh
out=$(CLAUDE_PROJECT_DIR=$work/o notice)
has "$(hook "$out" user)" "desligados no workspace org_O" && has "$(hook "$out" model)" "switched off"; check "agents switched off are reported, naming the workspace"
mkdir -p "$work/m"
printf '%s\torg_M\n' "$work/m" >> "$conf/projects"
user=$(hook "$(CLAUDE_PROJECT_DIR=$work/m notice)" user)
has "$user" "Falta o token do Unio do workspace org_M, que esta pasta usa (~/.config/unio/workspaces/org_M/token)"; check "a folder's missing workspace token is named"
rm -rf "$conf/projects" "$conf/workspaces"

mkdir -p "$HOME/.claude/skills/unio"
touch "$HOME/.claude/skills/unio/SKILL.md"
printf '{"mcpServers":{"unio":{"type":"http","url": "https://api.unio.so/v1/mcp","headers":{"Authorization":"Bearer %s"}}}}\n' \
  "$other" > "$HOME/.claude.json"
out=$(notice)
user=$(hook "$out" user)
has "$user" "skills/unio" && has "$user" ".claude.json"; check "the notice points at a hand-installed skill and server entry"
lacks "$out" "unio_[A-Za-z0-9]"; check "the notice never repeats a token it found"
out=$(UNIO_INBOX_RUN=1 notice)
[ -z "$out" ]; check "an inbox run gets no notice"

# The doctor. The fixture says 5 days left, so the token line is a warning.
before=$(calls)
chmod 644 "$conf/token"
report=$(doctor)
has "$report" "^warn core api: token aceito, mas vence em 5 dias" && [ "$(calls)" -eq $((before + 1)) ] &&
  [ "$(last_client)" = "claude-code/$version" ]; check "doctor checks the token with the client header and warns of the expiry"
has "$report" "^warn core version: $version; a mais nova é 1.1.0"; check "doctor reports the version behind"
has "$report" "^warn core token: .*chmod 600"; check "doctor flags a token file others can read"
chmod 620 "$conf/token"
has "$(doctor)" "^warn core token: .*chmod 600"; check "doctor flags a token file others can change"
chmod 400 "$conf/token"
has "$(doctor)" "^ok core token:"; check "doctor accepts a token file only its owner can read"
mv "$conf/token" "$tmp/linked-token"
chmod 600 "$tmp/linked-token"
ln -s "$tmp/linked-token" "$conf/token"
has "$(doctor)" "^ok core token:"; check "doctor judges a linked token file by its target"
rm -f "$conf/token"
mv "$tmp/linked-token" "$conf/token"
chmod 644 "$conf/token"
has "$report" "^warn migration user-skill" && has "$report" "^warn migration user-mcp" &&
  has "$report" "^warn migration static-token"; check "doctor reports the migration leftovers"
lacks "$report" "unio_[A-Za-z0-9]" && lacks "$report" Marker; check "doctor never prints a token or a name the server returned"
has "$report" "^info core plugin: carregado de uma pasta (--plugin-dir)" && lacks "$report" "core updates:"; check "doctor tells a plugin loaded from a folder"
mkdir -p "$tmp/dotfiles/claude/plugins/cache/unio/unio"
cp -R "$plugin" "$tmp/dotfiles/claude/plugins/cache/unio/unio/$version"
ln -s "$tmp/dotfiles/claude" "$tmp/linked-claude"
report=$(CLAUDE_CONFIG_DIR=$tmp/linked-claude "$shell" "$tmp/linked-claude/plugins/cache/unio/unio/$version/scripts/doctor.sh" claude-code)
lacks "$report" "plugin-dir" && has "$report" "core updates:"; check "doctor knows an installed plugin under a linked config folder"
days_left 1
out=$(connect "$good")
has "$out" "Ele vence em 1 dia\."; check "connect says a token's last day in the singular"
report=$(doctor)
has "$report" "^warn core api: token aceito, mas vence em 1 dia;"; check "doctor says a token's last day in the singular"
days_left 0
report=$(doctor)
has "$report" "^warn core api: token aceito, mas vence hoje;"; check "doctor says a token expires today"
expire status-default
refresh
user=$(hook "$(notice)" user)
has "$user" "O token do Unio deste computador vence hoje:"; check "the notice says a token expires today"
cp "$here/fixtures/skill_block.json" "$tmp/skill_block.json"
printf '%s' "$refused" > "$conf/token"
report=$(doctor)
has "$report" "^bad core api: o Unio recusou o token"; check "doctor reports a refused token"
rm -f "$conf/token"
report=$(doctor)
has "$report" "^bad core token: falta" && has "$report" "/unio:connect"; check "doctor sends a computer without a token to /unio:connect"
lacks "$report" "core folder:"; check "doctor checks the connection's folder only inside a session with mapped folders"

# Inside a session, on a computer with mapped folders, the doctor checks the
# folder the header helper will find.
mkdir -p "$tmp/mapped-a" "$tmp/mapped-b"
printf '%s\torg_A\n%s\torg_B\n' "$tmp/mapped-a" "$tmp/mapped-b" > "$conf/projects"
report=$(CLAUDECODE=1 agent "$tmp/mapped-a" "$tmp/mapped-a" "$shell" "$scripts/doctor.sh" claude-code)
has "$report" "^ok core folder: a conexão com o Unio usa o workspace desta pasta"; check "doctor confirms the connection sees this folder"
report=$(CLAUDECODE=1 agent "$tmp/mapped-b" "$tmp/mapped-a" "$shell" "$scripts/doctor.sh" claude-code)
has "$report" "^bad core folder: .*mapped-b, de outro workspace"; check "doctor reports a connection that sees another workspace's folder"
report=$(PATH=$tmp/no-ps:$PATH CLAUDECODE=1 agent "$tmp/mapped-a" "$tmp/mapped-a" "$shell" "$scripts/doctor.sh" claude-code)
has "$report" "^bad core folder: .*conecta sem token"; check "doctor reports a connection that cannot know its folder"
rm -f "$conf/projects"

# The installer, run as `curl ... | sh -s -- ...` runs it: the script on
# stdin, in a terminal of its own that types the answers (tests/pty_run.py).
# Each scenario gets a home folder of its own.
installer=$(CDPATH='' cd -- "$here/.." && pwd -P)/install.sh
has_text() { printf '%s' "$1" | grep -qF -e "$2"; }
last_line() { printf '%s\n' "$1" | tail -n 1; }
claude_calls() {
  if [ -f "$tmp/claude-argv.log" ]; then wc -l < "$tmp/claude-argv.log" | tr -d ' '; else echo 0; fi
}
home() {
  HOME=$tmp/homes/$1
  conf=$HOME/.config/unio
  mkdir -p "$conf"
  api "$port"
}
# What the person types: the token at its prompt, then each answer to the
# workspace question.
types() {
  printf 'Cole o token do Unio: \t%s\n' "$1" > "$tmp/answers"
  shift
  for answer; do printf 'Escolha 1 ou 2 [1]: \t%s\n' "$answer" >> "$tmp/answers"; done
}
# Runs the installer from folder $1 with the arguments that follow.
install_in() {
  dir=$1
  shift
  # Every install keeps a clone now, so every run gets the fake git: a real
  # one would reach GitHub.
  (cd "$dir" && PATH=$tmp/git-bin:$PATH python3 -I "$here/pty_run.py" "$installer" "$tmp/answers" -- "$shell" -s -- "$@") > "$tmp/transcript"
  result=$?
  cat "$tmp/transcript" >> "$tmp/transcripts.log"
  cat "$tmp/transcript"
  return "$result"
}
token_of() { cat "$conf/workspaces/$1/token" 2>/dev/null; }
helper_line="http_headers_helper = \"sh '$tmp/homes/codex/.local/share/unio/agent/plugins/unio/scripts/mcp-headers.sh' codex\""
mkdir -p "$tmp/repo" "$tmp/repo2" "$tmp/repo3"
ln -s "$tmp/repo2" "$tmp/linked-repo2"

floor=$(sed -n 's/^floor=//p' "$installer")
minimum=$(sed -n 's/.*"minimumClaudeCode"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$plugin/.claude-plugin/plugin.json")
[ -n "$floor" ] && [ "$floor" = "$minimum" ]; check "the installer's Claude Code floor is the plugin's minimumClaudeCode"

home args
types "$good"
before=$(claude_calls)
refused_all=1
for key in org_ org_a-b 'org_../x' ORG_x 'org_a b' "org_x$(printf '\033')"; do
  out=$(install_in "$tmp/repo" --workspace "$key")
  has_text "$out" "não é um workspace do Unio" || refused_all=0
done
[ "$refused_all" -eq 1 ] && [ "$(claude_calls)" -eq "$before" ] && [ ! -e "$conf/workspaces" ]; check "the installer refuses a workspace key outside ^org_[A-Za-z0-9]+\$"
out=$(install_in "$tmp/repo" --workspace org_test --client cursor)
has_text "$out" "--client aceita claude-code ou codex"; check "the installer names the clients it knows"
out=$(install_in "$tmp/repo" --workspace org_test --token "$good")
has_text "$out" "Opção desconhecida: --token." && lacks "$out" "unio_"; check "the installer refuses an unknown option without repeating what follows it"
out=$(CLAUDECODE=1 install_in "$tmp/repo" --workspace org_test)
has_text "$out" "fora do Claude Code" && lacks "$out" "Cole o token" && [ "$(claude_calls)" -eq "$before" ]; check "the installer refuses to run inside Claude Code"
out=$(cd "$tmp/repo" && python3 -I "$here/pty_run.py" --no-tty "$installer" -- "$shell" -s -- --workspace org_test 2>&1)
has_text "$out" "precisa de um terminal" && [ "$(claude_calls)" -eq "$before" ]; check "without a terminal, the installer stops before installing anything"

mkdir -p "$tmp/no-claude"
ln -s "$tmp/bin/curl" "$tmp/no-claude/curl"
if [ ! -e /usr/bin/claude ] && [ ! -e /bin/claude ]; then
  out=$(PATH=$tmp/no-claude:/usr/bin:/bin install_in "$tmp/repo" --workspace org_test)
  has_text "$out" "O comando claude não está no PATH. Instale o Claude Code $floor ou mais novo" && lacks "$out" "Cole o token"
else
  echo "(a claude command sits in /usr/bin or /bin)"
  false
fi; check "the installer stops when claude is not on PATH"
printf '2.1.200' > "$tmp/claude-version"
out=$(install_in "$tmp/repo" --workspace org_test)
code=$?
[ "$code" -eq 1 ] && has_text "$out" "O Claude Code 2.1.200 é anterior ao $floor" && lacks "$out" "Cole o token" &&
  ! grep -q '^plugin install' "$tmp/claude-argv.log" 2>/dev/null; check "the installer stops below the Claude Code floor"
rm -f "$tmp/claude-version"
touch "$tmp/claude-fails"
out=$(install_in "$tmp/repo" --workspace org_test)
code=$?
[ "$code" -eq 1 ] && has_text "$out" "Não foi possível instalar o plugin do Unio (Failed to add marketplace: network unreachable)" &&
  lacks "$out" "Cole o token" && [ ! -e "$conf/workspaces" ]; check "a failed plugin install stops the installer with claude's reason"
rm -f "$tmp/claude-fails"
home clone-fails
types "$good"
touch "$tmp/git-fails"
out=$(install_in "$tmp/repo" --workspace org_test)
code=$?
rm -f "$tmp/git-fails"
[ "$code" -eq 0 ] && [ "$(last_line "$out")" = "Conectado ao Unio." ] && has_text "$out" "Não foi possível baixar o Unio" &&
  [ ! -e "$HOME/.local/bin/unio" ]; check "a failed clone still installs and connects the plugin, without the unio command"

home fresh
types "$good"
before=$(calls)
out=$(install_in "$tmp/repo" --workspace org_test)
code=$?
[ "$code" -eq 0 ] && [ "$(last_line "$out")" = "Conectado ao Unio." ]; check "a first install ends with Conectado ao Unio."
[ -f "$HOME/.local/share/unio/agent/bin/unio" ] && [ "$(readlink "$HOME/.local/bin/unio")" = "$HOME/.local/share/unio/agent/bin/unio" ] &&
  has_text "$out" "Comando unio ligado em ~/.local/bin/unio."; check "a plugin install keeps a clone and links the unio command to it"
has_text "$out" "Claude Code 2.1.308 encontrado." && has_text "$out" "Plugin do Unio instalado." &&
  grep -qx 'plugin install unio --marketplace HL-legaltech/unio-agent' "$tmp/claude-argv.log"; check "the installer checks Claude Code and installs the plugin from the marketplace"
python3 -I -c '
import json, sys
entry = json.load(open(sys.argv[1]))["extraKnownMarketplaces"]["unio"]
sys.exit(0 if entry == {"source": {"source": "github", "repo": "HL-legaltech/unio-agent"}, "autoUpdate": True} else 1)' "$HOME/.claude/settings.json" &&
  has_text "$out" "Atualização automática do plugin ligada, em ~/.claude/settings.json."; check "the installer turns on auto-update in the entry Claude Code wrote"
has_text "$out" "Cole o token do Unio: " && [ "$(token_of org_test)" = "$good" ] && private "$conf/workspaces/org_test/token"; check "the token read at the hidden prompt is saved, private, in its workspace's folder"
[ -n "$(find "$conf" "$conf/workspaces" "$conf/workspaces/org_test" -prune -perm 700 | sed -n 3p)" ]; check "the installer's folders are private"
[ "$(cat "$conf/default-workspace")" = org_test ] && has_text "$out" "O workspace org_test vale em todas as pastas deste computador."; check "the first workspace becomes the computer's default"
[ "$(calls)" -eq $((before + 1)) ] && [ "$(last_client)" = "-" ]; check "the installer checks the token once, without the client header the page reads as the coding agent"
[ -z "$(find "$conf" -name '*.new')" ]; check "the installer leaves no temporary file"
has "$(headers "$tmp/elsewhere")" "Bearer $good"; check "the header helper sends the installed token from any folder"
has "$(doctor)" "^ok core token: ~/.config/unio/workspaces/org_test/token (o token do workspace org_test, o padrão deste computador)"; check "doctor calls the installer's first workspace the computer's default"
before=$(claude_calls)
types "$other"
out=$(install_in "$tmp/repo3" --workspace org_test)
[ "$(last_line "$out")" = "Conectado ao Unio." ] && has_text "$out" "O plugin do Unio já está instalado; a instalação foi pulada." &&
  has_text "$out" "A atualização automática do plugin já está ligada." && [ "$(token_of org_test)" = "$other" ] &&
  lacks "$out" "Escolha" && ! sed -n "$((before + 1)),\$p" "$tmp/claude-argv.log" | grep -q '^plugin install'; check "run again, the installer skips the install and only replaces the token"
types "$good"
out=$(install_in "$tmp/repo" --workspace org_test --renew)
[ "$(last_line "$out")" = "Conectado ao Unio." ] && [ "$(claude_calls)" -eq $((before + 2)) ] && [ "$(token_of org_test)" = "$good" ]; check "--renew calls no claude command and replaces the token"

types unio_short
before=$(calls)
out=$(install_in "$tmp/repo" --workspace org_test --renew)
code=$?
[ "$code" -eq 1 ] && has_text "$out" "Isso não é um token do Unio" && [ "$(calls)" -eq "$before" ] && [ "$(token_of org_test)" = "$good" ]; check "the installer refuses a malformed token without sending it"
[ "$(cat "$tmp/answers.echo")" = on ]; check "the terminal echoes again after the hidden prompt"
types '<interrupt>'
out=$(install_in "$tmp/repo" --workspace org_test --renew)
code=$?
[ "$code" -eq 130 ] && [ "$(cat "$tmp/answers.echo")" = on ] && [ "$(token_of org_test)" = "$good" ]; check "Ctrl-C at the hidden prompt turns echo back on and saves nothing"
api "$closed"
types "$other"
out=$(install_in "$tmp/repo" --workspace org_test --renew)
has_text "$out" "O Unio não respondeu: confira a rede" && [ "$(token_of org_test)" = "$good" ]; check "the installer tells a network failure (000) apart and saves nothing"
api "$port"
types "$refused"
out=$(install_in "$tmp/repo" --workspace org_test --renew)
has_line "$out" "O Unio recusou o token. Gere outro na página Agente de código e rode o comando de novo." && [ "$(token_of org_test)" = "$good" ]; check "the installer tells a refused token (401) apart and saves nothing"
types "$off"
out=$(install_in "$tmp/repo" --workspace org_test --renew)
has_text "$out" "desligados neste workspace" && [ "$(token_of org_test)" = "$good" ]; check "the installer reports agents switched off (403) and saves nothing"
types "$second"
out=$(install_in "$tmp/repo" --workspace org_test --renew)
has_text "$out" "Este token é do workspace org_Second, mas o comando é para o org_test. Nada foi salvo." &&
  [ "$(token_of org_test)" = "$good" ] && [ ! -e "$conf/workspaces/org_Second" ]; check "the installer refuses another workspace's token and saves nothing"

types "$second" 1
out=$(install_in "$tmp/linked-repo2" --workspace org_Second)
has_text "$out" "Este computador já usa o Unio com outro workspace. Onde usar o workspace org_Second?" &&
  has_text "$out" "1. Só nesta pasta: $tmp/repo2" && [ "$(token_of org_Second)" = "$second" ] &&
  [ "$(cat "$conf/default-workspace")" = org_test ]; check "a second workspace asks where it applies"
printf '%s\torg_Second\n%s\torg_Second\n' "$tmp/repo2" "$tmp/linked-repo2" | cmp -s - "$conf/projects" &&
  has_text "$out" "O workspace org_Second vale só em $tmp/repo2 e nas pastas dentro dela."; check "answered 1, it maps this folder, by its real path and by the link it was reached through"
has "$(headers "$tmp/repo2")" "Bearer $second" && has "$(headers "$tmp/elsewhere")" "Bearer $good"; check "the mapped folder sends the second workspace's token, other folders the first's"
has "$(CLAUDE_PROJECT_DIR=$tmp/repo2 doctor)" "^ok core token: .*(o token do workspace org_Second, ligado a esta pasta)"; check "doctor says a mapped folder's workspace is tied to the folder"
types "$second" x ''
out=$(install_in "$tmp/repo3" --workspace org_Second)
[ "$(printf '%s\n' "$out" | grep -c 'Escolha 1 ou 2')" -eq 2 ] && grep -q "^$tmp/repo3	org_Second\$" "$conf/projects" &&
  grep -q "^$tmp/repo2	org_Second\$" "$conf/projects"; check "an unknown answer asks again, and Enter keeps the folder"
types "$second"
out=$(install_in "$tmp/elsewhere" --workspace org_Second --renew)
lacks "$out" "Escolha" && [ "$(cat "$conf/default-workspace")" = org_test ] && [ "$(wc -l < "$conf/projects" | tr -d ' ')" -eq 3 ]; check "--renew of a workspace this computer knows changes no folder"
types "$second" 2
out=$(install_in "$tmp/elsewhere" --workspace org_Second)
[ "$(cat "$conf/default-workspace")" = org_Second ] && has_text "$out" "passa a valer em todas as pastas deste computador" &&
  has "$(headers "$tmp/elsewhere")" "Bearer $second"; check "answered 2, the second workspace becomes the computer's default"

home legacy
printf '%s\n' "$good" > "$conf/token"
types "$other"
out=$(install_in "$tmp/repo" --workspace org_test --renew)
lacks "$out" "Escolha" && [ "$(cat "$conf/default-workspace")" = org_test ] &&
  has_text "$out" "O token em ~/.config/unio/token deixa de ser lido."; check "a computer token of the same workspace is taken over without a question"
home legacy-other
printf '%s\n' "$second" > "$conf/token"
types "$good" 1
out=$(install_in "$tmp/repo" --workspace org_test --renew)
has_text "$out" "Onde usar o workspace org_test?" && [ ! -e "$conf/default-workspace" ] && has "$(headers "$tmp/elsewhere")" "Bearer $second" &&
  has "$(headers "$tmp/repo")" "Bearer $good"; check "a computer token of another workspace counts as a first workspace"

home settings
mkdir -p "$HOME/.claude"
touch "$HOME/.claude/fake-installed"
cat > "$HOME/.claude/settings.json" <<'JSON'
{
  "permissions": {
    "allow": [
      "mcp__plugin_unio_unio__*"
    ]
  },
  "extraKnownMarketplaces": {
    "unio": {
      "source": {
        "source": "github",
        "repo": "HL-legaltech/unio-agent"
      }
    },
    "other": {
      "source": {
        "source": "github",
        "repo": "example/other"
      }
    }
  }
}
JSON
types "$good"
out=$(install_in "$tmp/repo" --workspace org_test)
python3 -I -c '
import json, sys
data = json.load(open(sys.argv[1]))
known = data["extraKnownMarketplaces"]
ok = known["unio"]["autoUpdate"] is True and "autoUpdate" not in known["other"] and data["permissions"]["allow"] == ["mcp__plugin_unio_unio__*"]
sys.exit(0 if ok else 1)' "$HOME/.claude/settings.json"; check "auto-update is turned on beside other settings and marketplaces"
printf '{"extraKnownMarketplaces":{"unio":{"source":{"source":"github","repo":"HL-legaltech/unio-agent"}}}}\n' > "$HOME/.claude/settings.json"
cp "$HOME/.claude/settings.json" "$tmp/settings.copy"
touch "$HOME/.claude/fake-disabled"
out=$(install_in "$tmp/repo" --workspace org_test)
cmp -s "$HOME/.claude/settings.json" "$tmp/settings.copy" &&
  has_text "$out" "Ligue a atualização automática do plugin: no Claude Code, /plugin > Marketplaces > unio > Enable auto-update."; check "settings in a shape Claude Code does not write are left alone, and the installer says how"
has_text "$out" "claude plugin enable unio@unio"; check "the installer says when the plugin is installed but turned off"
mv "$HOME/.claude/settings.json" "$tmp/linked-settings.json"
ln -s "$tmp/linked-settings.json" "$HOME/.claude/settings.json"
out=$(install_in "$tmp/repo" --workspace org_test)
[ -L "$HOME/.claude/settings.json" ] && has_text "$out" "Ligue a atualização automática"; check "a linked settings file is never replaced"

home codex
mkdir -p "$HOME/.codex"
before=$(claude_calls)
types "$good"
out=$(PATH=$tmp/git-bin:$PATH install_in "$tmp/repo" --workspace org_test --client codex)
[ "$(last_line "$out")" = "Conectado ao Unio." ] && [ "$(claude_calls)" -eq "$before" ] && [ "$(token_of org_test)" = "$good" ]; check "Codex: the installer calls no claude command and saves the token"
grep -qx "clone --quiet https://github.com/HL-legaltech/unio-agent.git $HOME/.local/share/unio/agent" "$tmp/git-argv.log"; check "Codex: the installer clones the repository to ~/.local/share/unio/agent"
[ "$(CDPATH='' cd -P -- "$HOME/.agents/skills/unio" && pwd -P)" = "$HOME/.local/share/unio/agent/plugins/unio/skills/unio" ]; check "Codex: the skill is linked into ~/.agents/skills/unio"
has_line "$out" "$helper_line" && has_line "$out" "url = \"http://127.0.0.1:$port/v1/mcp\"" && has_line "$out" "[mcp_servers.unio]"; check "Codex: the config block names the real home folder"
out=$(cd "$tmp/elsewhere" && "$shell" "$HOME/.local/share/unio/agent/plugins/unio/scripts/mcp-headers.sh" codex)
has "$out" "Bearer $good" && has "$out" "codex/$version"; check "Codex: the helper the block names sends the token"
printf '\n[mcp_servers.unio]\n%s\n' "$helper_line" >> "$HOME/.codex/config.toml"
out=$(PATH=$tmp/git-bin:$PATH install_in "$tmp/repo" --workspace org_test --client codex)
grep -qx "\-C $HOME/.local/share/unio/agent pull --quiet --ff-only" "$tmp/git-argv.log" && has_text "$out" "A skill do Unio já está em ~/.agents/skills/unio." &&
  has_text "$out" "O Codex já lê o token pelo helper do Unio" && lacks "$out" "Acrescente este bloco"; check "Codex, run again: the clone is pulled, the link and the config are kept"
home codex-taken
mkdir -p "$HOME/.local/share/unio/agent"
out=$(PATH=$tmp/git-bin:$PATH install_in "$tmp/repo" --workspace org_test --client codex)
has_text "$out" "não é um clone do Unio" && lacks "$out" "Cole o token"; check "Codex: a folder in the clone's place stops the installer"

# The answering service: --runner clones as Codex does, then hands the
# terminal to the clone's runner/unio-runner, here a stub that logs how it
# was called. unio-runner's own tests are in runner/tests.
runner_calls() {
  if [ -f "$tmp/runner-argv.log" ]; then wc -l < "$tmp/runner-argv.log" | tr -d ' '; else echo 0; fi
}
home runner-args
before=$(claude_calls)
out=$(PATH=$tmp/git-bin:$PATH install_in "$tmp/repo" --runner)
code=$?
[ "$code" -eq 2 ] && has_text "$out" "--runner precisa de --workspace org_" && [ ! -e "$HOME/.local/share/unio" ]; check "--runner without --workspace stops before cloning"
out=$(PATH=$tmp/git-bin:$PATH install_in "$tmp/repo" --runner --workspace org_test --renew)
code=$?
[ "$code" -eq 2 ] && has_text "$out" "--runner não combina com --renew" && [ ! -e "$HOME/.local/share/unio" ]; check "--runner refuses --renew"
mkdir -p "$tmp/fake-windows"
printf '#!/bin/sh\necho MINGW64_NT-10.0-26100\n' > "$tmp/fake-windows/uname"
chmod +x "$tmp/fake-windows/uname"
out=$(PATH=$tmp/fake-windows:$tmp/git-bin:$PATH install_in "$tmp/repo" --runner --workspace org_test)
has_text "$out" "No Windows, rode o comando do PowerShell" && [ ! -e "$HOME/.local/share/unio" ]; check "--runner under Git Bash points at the PowerShell command"
home runner-missing
out=$(PATH=$tmp/git-bin:$PATH install_in "$tmp/repo" --runner --workspace org_test)
code=$?
[ "$code" -eq 1 ] && has_text "$out" "ainda não tem o serviço de respostas" && [ "$(runner_calls)" -eq 0 ] &&
  [ ! -e "$HOME/.local/bin/unio" ]; check "--runner stops when the clone has no runner yet, before linking the command"

mkdir -p "$tmp/runner-stub"
cat > "$tmp/runner-stub/unio-runner" <<STUB
import os, sys
with open("$tmp/runner-argv.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + " tty=%d\n" % os.isatty(0))
print("unio-runner setup chamado.")
STUB
printf '9.9.9\n' > "$tmp/runner-stub/VERSION"
home runner
out=$(PATH=$tmp/git-bin:$PATH install_in "$tmp/repo" --runner --workspace org_test)
code=$?
[ "$code" -eq 0 ] && [ "$(last_line "$out")" = "unio-runner setup chamado." ] && has_text "$out" "Unio baixado em ~/.local/share/unio/agent." &&
  grep -qx "clone --quiet https://github.com/HL-legaltech/unio-agent.git $HOME/.local/share/unio/agent" "$tmp/git-argv.log"; check "--runner clones the repository as Codex does, then runs the clone's unio-runner"
[ "$(tail -n 1 "$tmp/runner-argv.log")" = "setup --workspace org_test --api http://127.0.0.1:$port/v1 tty=1" ]; check "--runner runs setup for the workspace, on this computer's API, with the terminal as stdin"
lacks "$out" "Cole o token" && [ "$(claude_calls)" -eq "$before" ] && [ ! -e "$conf/workspaces" ] && [ ! -e "$HOME/.agents" ]; check "--runner installs no plugin, links no skill and stores no coding agent token"
[ -L "$HOME/.local/bin/unio" ] && [ "$(readlink "$HOME/.local/bin/unio")" = "$HOME/.local/share/unio/agent/bin/unio" ] &&
  has_text "$out" "Comando unio ligado em ~/.local/bin/unio."; check "--runner links ~/.local/bin/unio to the clone's bin/unio"
# shellcheck disable=SC2016 # the line the installer prints, unexpanded
has_text "$out" "A pasta ~/.local/bin não está no PATH deste terminal" && has_text "$out" 'export PATH="$HOME/.local/bin:$PATH"'; check "--runner says when ~/.local/bin is not on PATH, and how to add it"
out=$(PATH=$HOME/.local/bin:$tmp/git-bin:$PATH install_in "$tmp/repo" --runner --workspace org_test)
grep -qx "\-C $HOME/.local/share/unio/agent pull --quiet --ff-only" "$tmp/git-argv.log" && [ "$(runner_calls)" -eq 2 ] &&
  has_text "$out" "Unio atualizado em ~/.local/share/unio/agent."; check "--runner, run again, pulls the clone and runs setup again"
has_text "$out" "O comando unio já está em ~/.local/bin/unio." && lacks "$out" "não está no PATH" &&
  [ "$(readlink "$HOME/.local/bin/unio")" = "$HOME/.local/share/unio/agent/bin/unio" ]; check "--runner, run again, keeps its link and says nothing of a PATH that has ~/.local/bin"
mkdir -p "$tmp/old-python"
cat > "$tmp/old-python/python3" <<STUB
#!/bin/sh
case "\$*" in *version_info*) exit 1 ;; esac
exec "$(command -v python3)" "\$@"
STUB
chmod +x "$tmp/old-python/python3"
out=$(PATH=$tmp/old-python:$tmp/git-bin:$PATH install_in "$tmp/repo" --runner --workspace org_test)
code=$?
[ "$code" -eq 1 ] && has_text "$out" "precisa do Python 3.9 ou mais novo" && [ "$(runner_calls)" -eq 2 ]; check "--runner stops on a Python older than 3.9"
out=$("$shell" "$HOME/.local/bin/unio" --version) && [ "$out" = 9.9.9 ]; check "the linked unio finds its clone through the link"
(cd "$tmp/elsewhere" && "$shell" "$HOME/.local/bin/unio" runner status --workspace org_test </dev/null >/dev/null) &&
  [ "$(tail -n 1 "$tmp/runner-argv.log")" = "status --workspace org_test tty=0" ]; check "unio runner hands the rest to the clone's unio-runner"
"$shell" "$HOME/.local/bin/unio" bogus >/dev/null 2>&1
[ $? -eq 2 ]; check "unio refuses a command it does not have"
home runner-taken
mkdir -p "$HOME/.local/bin"
ln -s /nonexistent/unio "$HOME/.local/bin/unio"
out=$(PATH=$tmp/git-bin:$PATH install_in "$tmp/repo" --runner --workspace org_test)
# shellcheck disable=SC2088 # the text the installer prints
[ "$(readlink "$HOME/.local/bin/unio")" = /nonexistent/unio ] && [ "$(last_line "$out")" = "unio-runner setup chamado." ] &&
  has_text "$out" "~/.local/bin/unio já existe e não é o comando unio deste clone, então ficou como estava." &&
  has_text "$out" "O comando do Unio está em ~/.local/share/unio/agent/bin/unio." && lacks "$out" "não está no PATH"; check "--runner leaves a ~/.local/bin/unio that points elsewhere, says so, and goes on"
rm -f "$HOME/.local/bin/unio"
printf 'mine\n' > "$HOME/.local/bin/unio"
out=$(PATH=$tmp/git-bin:$PATH install_in "$tmp/repo" --runner --workspace org_test)
[ "$(cat "$HOME/.local/bin/unio")" = mine ] && has_text "$out" "já existe e não é o comando unio deste clone"; check "--runner never replaces a file named ~/.local/bin/unio"

# The browser approval (connect.sh browser and the installer without
# --workspace), against the fake's scripted polls. The fake asks for a poll
# a second; each scenario gets a home folder of its own.
approval() { printf '%s\n' "$@" > "$tmp/connect/polls"; }
polls() { n=$(grep -c '^/v1/agents/connect/poll ' "$tmp/connect/log" 2>/dev/null); printf '%s' "${n:-0}"; }
# connect.sh browser from folder $1, with no terminal, as Claude Code's own
# shell would run it.
approve_in() {
  (cd "$1" && python3 -I "$here/pty_run.py" --no-tty /dev/null -- "$shell" "$scripts/connect.sh" browser) 2>&1
}

home browser
rm -f "$tmp/connect/log" "$tmp/xdg-open.log"
approval pending "approved $good"
out=$(DISPLAY=:0 approve_in "$tmp/repo")
code=$?
[ "$code" -eq 0 ] && has "$out" "confira o código BCDF-GHJK" && has_line "$out" "  https://unio.so/app/connect?code=BCDF-GHJK" &&
  has "$out" "Aprovado no Unio\." && [ "$(polls)" -eq 2 ]; check "browser: connect shows the page and the code, polls while pending, and is approved"
[ "$(token_of org_test)" = "$good" ] && private "$conf/workspaces/org_test/token" && [ "$(cat "$conf/default-workspace")" = org_test ] &&
  has "$out" "Token salvo em ~/.config/unio/workspaces/org_test/token, para o workspace WorkspaceNameMarker" &&
  has "$out" "Ele vence em 30 dias\." && has "$out" "vale em todas as pastas deste computador"; check "browser: the first workspace's token is saved as the installer saves it, the computer's default"
[ -z "$(find "$conf" -name '*.new')" ] && [ -z "$(find "$conf" -name 'token.new.*')" ]; check "browser: no temporary file is left"
grep -qx 'https://unio.so/app/connect?code=BCDF-GHJK' "$tmp/xdg-open.log"; check "browser: the page opens in a browser on this screen"
grep -q '^/v1/agents/connect/start [0-9.]* {"client":"claude-code","host":"[A-Za-z0-9][A-Za-z0-9_-]*"}$' "$tmp/connect/log"; check "browser: start names the client and a host name"
lacks "$out" "unio_" && lacks "$out" "FakeDeviceCode" &&
  ! grep -q -e "$good" -e FakeDeviceCode "$tmp/curl-argv.log"; check "browser: neither the token nor the device code is printed or on curl's command line"
has "$(headers "$tmp/elsewhere")" "Bearer $good"; check "browser: the header helper sends the approved token"

rm -f "$tmp/connect/log" "$tmp/xdg-open.log"
approval slow_down "approved $second"
out=$(approve_in "$tmp/repo2")
gap=$(grep '^/v1/agents/connect/poll ' "$tmp/connect/log" | awk '{ t[NR] = $2 } END { printf "%d", t[NR] - t[NR - 1] }')
[ "$(polls)" -eq 2 ] && [ "$gap" -ge 6 ]; check "browser: slow_down adds five seconds to the interval"
[ "$(token_of org_Second)" = "$second" ] && [ "$(cat "$conf/default-workspace")" = org_test ] &&
  grep -q "^$tmp/repo2	org_Second\$" "$conf/projects" && has "$out" "vale só nesta pasta"; check "browser: a second workspace, with no terminal to ask, covers only this folder"
[ ! -e "$tmp/xdg-open.log" ]; check "browser: without a display, no browser is started"

home browser-refused
approval denied
out=$(approve_in "$tmp/repo")
code=$?
[ "$code" -ne 0 ] && has "$out" "recusado no navegador" && [ ! -e "$conf/workspaces" ]; check "browser: a refused request saves nothing"
approval gone
out=$(approve_in "$tmp/repo")
has "$out" "venceu ou já foi usado" && [ ! -e "$conf/workspaces" ]; check "browser: an expired or used request (410) saves nothing"
printf '2\n' > "$tmp/connect/ttl"
approval
out=$(approve_in "$tmp/repo")
rm -f "$tmp/connect/ttl"
has "$out" "venceu sem aprovação" && [ ! -e "$conf/workspaces" ]; check "browser: connect stops polling when the request's time is up"
printf '404\n' > "$tmp/connect/start"
rm -f "$tmp/connect/log"
out=$(approve_in "$tmp/repo")
rm -f "$tmp/connect/start"
has "$out" "ainda não está disponível" && has "$out" "Configurações > Pessoal > Agente de código" && [ "$(polls)" -eq 0 ]; check "browser: before Unio offers it, connect points at the page"
rm -f "$tmp/connect/log"
out=$(CLAUDECODE=1 approve_in "$tmp/repo")
has "$out" "fora do Claude Code" && [ ! -e "$tmp/connect/log" ]; check "browser: connect refuses to run inside Claude Code"

home browser-install
: > "$tmp/answers"
approval pending "approved $good"
out=$(install_in "$tmp/repo")
[ "$(last_line "$out")" = "Conectado ao Unio." ] && has_text "$out" "Plugin do Unio instalado." && has_text "$out" "Aprovado no Unio." &&
  lacks "$out" "Cole o token" && [ "$(token_of org_test)" = "$good" ]; check "the installer without --workspace installs and connects through the browser"
approval "approved $good"
out=$(install_in "$tmp/repo" --workspace org_Second --browser --renew)
has_text "$out" "aprovado no workspace org_test, e não no org_Second do comando" && [ ! -e "$conf/workspaces/org_Second" ] &&
  [ "$(token_of org_test)" = "$good" ]; check "--browser: a request approved in another workspace is saved for that workspace, and said"
approval denied
out=$(install_in "$tmp/repo" --browser --renew)
has_text "$out" "recusado no navegador" && [ "$(token_of org_test)" = "$good" ]; check "--browser: a refusal keeps the token there was"

! grep -q -e "$good" -e "$other" -e "$refused" -e "$off" -e "$second" "$tmp/claude-argv.log" "$tmp/git-argv.log" &&
  ! grep -q 'unio_[A-Za-z0-9]\{20\}' "$tmp/transcripts.log"; check "no token was ever on claude's or git's command line, or on the installer's terminal"
! grep -i -e 'cannot ' -e 'not found' -e 'no such file' -e 'syntax error' -e 'unexpected' -e 'permission denied' "$tmp/transcripts.log"; check "the installer's terminal shows no shell or command error"

! grep -q -e "$good" -e "$other" -e "$refused" -e "$off" -e "$second" "$tmp/curl-argv.log"; check "no token was ever on curl's command line"

echo "$((checks - fails)) of $checks checks passed under $shell"
[ "$fails" -eq 0 ]
