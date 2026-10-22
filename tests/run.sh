#!/bin/sh
# Runs the plugin's scripts against tests/fake_api.py in a throwaway HOME: no
# real token, config or API is touched, and nothing leaves the machine. The
# scripts run under the shell that runs this file, so CI runs it twice:
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

tmp=$(mktemp -d "${TMPDIR:-/tmp}/unio-tests.XXXXXX")
server=
trap 'if [ -n "$server" ]; then kill "$server" 2>/dev/null; fi; rm -rf "$tmp"' EXIT
trap 'exit 130' INT TERM HUP

unset CLAUDECODE CLAUDE_CONFIG_DIR CLAUDE_PROJECT_DIR UNIO_INBOX_RUN XDG_STATE_HOME
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
printf '#!/bin/sh\necho "2.1.308 (Claude Code)"\n' > "$tmp/bin/claude"
chmod +x "$tmp/bin/curl" "$tmp/bin/claude"
PATH=$tmp/bin:$PATH

free_port() {
  python3 -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}
port=$(free_port)
closed=$(free_port)
cp "$here/fixtures/skill_block.json" "$tmp/skill_block.json"
python3 -I "$here/fake_api.py" "$port" "$tmp/clients.log" "$tmp/skill_block.json" \
  "$refused" "$off" "$good" "$other" 2>"$tmp/server.err" &
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
headers() { (cd "$1" && "$shell" "$scripts/mcp-headers.sh" "${2:-claude-code}"); }
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
shaped=$(printf '%s\n' "$good" "$other" "$refused" "$off" | grep -cx 'unio_[0-9A-Za-z]\{46\}')
[ "$shaped" -eq 4 ]; check "the fake tokens have the real shape"

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
rm -f "$conf/default-workspace" "$conf/projects" "$conf/token"
rm -rf "$conf/workspaces"

# The connect script.
out=$("$shell" "$scripts/connect.sh" instructions)
has "$out" "Configurações > Sistema > Agentes > Gerar token"; check "connect names where tokens come from"
has_line "$out" "sh $scripts/connect.sh" && has "$out" "Nunca cole o token neste chat"; check "connect prints the command to run elsewhere"
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
has "$out" "outro workspace (~/.config/unio/workspaces/org_A/token)" && has "$out" "só para as pastas fora de ~/.config/unio/projects"; check "connect says which folders the token it saves serves"
printf 'org_A\n' > "$conf/default-workspace"
out=$("$shell" "$scripts/connect.sh" instructions)
has "$out" "nenhuma pasta lê ~/.config/unio/token e o comando se recusa" && lacks "$out" "demais pastas"; check "connect says no folder reads the computer's token while default-workspace exists"
before=$(calls)
out=$(connect "$good")
has "$out" "default-workspace escolhe o workspace org_A" && [ ! -e "$conf/token" ] && [ "$(calls)" -eq "$before" ]; check "connect saves no token that no folder would read"
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

out=$(notice)
[ "$(calls)" -eq $((before + 1)) ]; check "the notice reads local files only"
user=$(hook "$out" user)
model=$(hook "$out" model)
is_json "$out"; check "the notice is JSON after a check"
has "$user" "vence em 5 dias"; check "the notice warns the token expires within 7 days"
has "$user" "Saiu a versão 1.1.0" && has "$user" "/unio:update"; check "the notice announces a newer release"
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

serve '{"current":"1.1","minimum":"1.0","yours":"1.0.0","status":"behind","updateUrl":"https://github.com/HL-legaltech/unio-agent#update"}'
expire status-default
refresh
user=$(hook "$(notice)" user)
grep -qx current=1.1 "$status" && grep -qx minimum=1.0 "$status" && has "$user" "Saiu a versão 1.1 "; check "two-part versions from the server are read"
serve '{"current":"1.2.0","minimum":"1.2","yours":"1.0.0","status":"unsupported","updateUrl":"https://github.com/HL-legaltech/unio-agent#update"}'
expire status-default
refresh
out=$(notice)
user=$(hook "$out" user)
model=$(hook "$out" model)
has "$user" "abaixo da versão mínima que o Unio aceita (1.2)" && has "$model" "below the minimum"; check "below the minimum, the person and Claude are told to update"
cp "$tmp/my plugins/unio/.claude-plugin/plugin.json" "$tmp/plugin.json"
sed 's/"version": *"[^"]*"/"version": "1.2.0"/' "$tmp/plugin.json" > "$tmp/my plugins/unio/.claude-plugin/plugin.json"
user=$(hook "$("$shell" "$tmp/my plugins/unio/scripts/session-start.sh" notice)" user)
has "$user" "atualizado para 1.2.0" && lacks "$user" "abaixo da versão mínima"; check "after an update, the old version's verdict is not repeated"
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
has "$user" "recusou o token" && has "$model" "refused"; check "a refused token is reported to the person and to Claude"
printf '%s' "$good" > "$conf/workspaces/org_R/token"
expire status-org_R
user=$(hook "$(CLAUDE_PROJECT_DIR=$work/r notice)" user)
lacks "$user" "recusou"; check "a new token silences the old refusal"
CLAUDE_PROJECT_DIR=$work/o refresh
out=$(CLAUDE_PROJECT_DIR=$work/o notice)
has "$(hook "$out" user)" "desligados" && has "$(hook "$out" model)" "switched off"; check "agents switched off are reported"
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
has "$user" "Seu token do Unio vence hoje:"; check "the notice says a token expires today"
cp "$here/fixtures/skill_block.json" "$tmp/skill_block.json"
printf '%s' "$refused" > "$conf/token"
report=$(doctor)
has "$report" "^bad core api: o Unio recusou o token"; check "doctor reports a refused token"
rm -f "$conf/token"
report=$(doctor)
has "$report" "^bad core token: falta" && has "$report" "/unio:connect"; check "doctor sends a computer without a token to /unio:connect"

! grep -q -e "$good" -e "$other" -e "$refused" -e "$off" "$tmp/curl-argv.log"; check "no token was ever on curl's command line"

echo "$((checks - fails)) of $checks checks passed under $shell"
[ "$fails" -eq 0 ]
