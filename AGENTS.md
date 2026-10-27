# AGENTS.md

Rules for people and coding agents working in this repository. The repository
is public: every file, commit and pull request here is visible to anyone.

## What this is

- `.claude-plugin/marketplace.json`: the `unio` marketplace, with one plugin.
- `plugins/unio/`: the `unio` plugin for Claude Code. Its skill
  (`skills/unio/SKILL.md`) and its header helper (`scripts/mcp-headers.sh`)
  also serve Codex from a clone of this repository.
- `install.sh`: the installer the Agente de código page in Unio runs with
  `curl … | sh`. It installs the plugin (or, for Codex, the clone) and stores
  a token per workspace. On Windows the page runs `install.ps1`, which checks
  Git for Windows and Claude Code, then runs `install.sh` with Git's `sh`.
- `tests/`: the plugin's scripts and the installer, run against a fake Unio
  API in a throwaway HOME.
- `runner/`: the answering service (`unio-inbox`) and its setup
  (`unio-runner`), which run on the computer of a person in the workspace;
  see [runner/README.md](runner/README.md). It is not part of the plugin, so
  a change there raises no plugin version. `install.sh --runner` and
  `install.ps1 -Runner` (Windows) install it.
- `bin/unio` (POSIX `sh`) and `bin/unio.cmd` (Windows): the `unio` command
  people type, which runs from the clone it sits in. `unio runner …` is
  `runner/unio-runner`; text people read says `unio runner`, never
  `unio-runner`. Every install keeps the clone and links the command as
  `~/.local/bin/unio`; on Windows only `install.ps1` puts it on the PATH,
  by adding the clone's `bin` folder to the user `Path`.

Merging to `main` is the release: Claude Code installs and updates the plugin
from the default branch, and Codex clones pull it.

## Merging

- Agents never merge here, whatever another guide allows. This overrides
  section 6 of the organization's
  [contribution guide](https://github.com/HL-legaltech/.github-private/blob/main/CONTRIBUTING.md)
  (members only), which otherwise applies.
- Every change reaches `main` through a pull request that the code owner in
  `.github/CODEOWNERS` reviews and merges.
- Never push to `main`, force-push, or rewrite published history.

## Versions

- Every change under `plugins/unio/` raises `version` in
  `plugins/unio/.claude-plugin/plugin.json` and adds a `## <version>` section
  at the top of `plugins/unio/CHANGELOG.md`.
- `MAJOR.MINOR` of that version is the skill's version. A change to the skill
  text raises MINOR (or MAJOR) and moves both the skill's `metadata.version`
  and its line "This is version X.Y of the Unio skill." to the new number. A
  release that leaves the skill alone raises PATCH only.
- Unio's server rates each client by this number (`X-Unio-Client`), so a
  version is never reused or lowered.
- Never set `version` in `marketplace.json`: `plugin.json` wins and the two
  would disagree.
- The CHANGELOG's first section is what a person sees after an update, inside
  a Portuguese notice. Write each section for them, in a few lines of
  Brazilian Portuguese.

## Checks

CI (`.github/workflows/ci.yml`) runs these on every pull request. Run them
before opening one:

```sh
claude plugin validate --strict .
claude plugin validate --strict plugins/unio
git ls-files -z '*.sh' | xargs -0 shellcheck -s sh
shellcheck -s sh bin/unio
python3 -I tests/check_fixture.py
dash tests/run.sh
bash tests/run.sh
sh tests/check_versions.sh origin/main
python3 -I tests/e2e/claude_session.py
python3 -m unittest discover -s runner/tests -v
```

- `tests/e2e/claude_session.py` runs the real Claude Code with the plugin
  against fake Anthropic and Unio APIs on loopback, and CI runs it on Linux,
  macOS and Windows. It is the only check of what Claude Code itself does
  with the hook, the commands and the header helper on each system.
- A change to a command or to the hook also needs a real run of each command
  it touches, in the default permission mode, from a throwaway config folder
  you are signed in to: `CLAUDE_CONFIG_DIR=<folder> claude -p --plugin-dir
  plugins/unio "/unio:doctor"`. Only that run proves an injected script is
  allowed to run.
- `tests/fixtures/skill_block.json` is copied verbatim into the backend's
  spec of `get_me`. Change it only together with that spec.

## Commits

Conventional Commits, in English: `type(scope): subject`, imperative, lower
case, no final period, with a body wrapped at 72 columns that says what was
wrong, what changed and why. The last line is the task trailer, `Unio: <KEY>`.
No AI attribution. Stage named paths only.

## Secrets and names

- Never commit a token, an environment value, or a real `~/.config/unio` file.
  Tests use the fake tokens that `tests/run.sh` builds.
- No hostnames other than `unio.so` and `api.unio.so`, and no person's or
  customer's name, except the code owner in `.github/CODEOWNERS`.

## Traps

- The plugin's scripts are POSIX `sh`: they run under dash, bash, macOS `sh`
  and Git Bash. They call POSIX utilities, plus `curl`, `mktemp` and
  `date +%s`, which every supported system has; `lsof` on macOS and
  `cygpath` and `powershell.exe` on Windows, where `UNIO_OS` says so. Check
  any other tool on macOS and Git Bash before a script depends on it.
- On Windows Claude Code runs a hook in exec form (`args`) only when its
  `command` is an `.exe` on the PATH, and Git for Windows does not put
  `sh.exe` there. Hooks stay in shell form, which Claude Code runs through
  Git Bash.
- Claude Code starts the header helper in the plugin's folder, with no
  `CLAUDE_PROJECT_DIR` and no session id. The folder comes from
  `unio_agent_dir`; never from the helper's own working directory.
- `install.sh` runs piped into `sh`, so it cannot source `lib.sh`: its copies
  of `lib.sh`'s helpers must change with them. Its stdin is the script
  itself, so it reads answers only from `/dev/tty` and starts `claude` and
  `git` with stdin from `/dev/null`.
- A token never reaches a process's argv. It leaves a script only through the
  `printf` builtin, and `curl` reads it from stdin with `-K -`.
- A script that a command skill injects with `` !`…` `` must exit 0, or Claude
  Code aborts the command. Its `allowed-tools` rule must match the injected
  line exactly, or the command stops with "Shell command permission check
  failed" in the default permission mode.
- The session hook's `additionalContext` carries fixed text only, never a name
  or CHANGELOG text: what someone types in Unio must not reach Claude's context
  through the hook.
- The session status (`scripts/session-status.sh` and `scripts/unio_sessions/`)
  is standard-library Python 3.9 or later and prints nothing: Python 3.9 reads
  no `Z` in `datetime.fromisoformat`, so times go through `watch.stamp`. On
  Linux and macOS its state folder, file names and lock names are those of the
  hand-made hook it replaced, so a session that hook already watches is never
  watched twice. Without Python it reports nothing.
- Text a person reads at runtime (hook messages, command output) is Brazilian
  Portuguese: sentence case, no em dash, no emoji. Repository files are in
  English.
- `unio help` prints the lines of `bin/unio` that start with `#:`, the one
  table of commands, and `bin/unio.cmd` prints the same lines. A new command
  adds its line there and a branch to each file; the table lists only
  commands both files run.
- `bin/unio.cmd` runs every command but `runner` as `bin/unio`, through Git
  for Windows' `sh.exe`, which is not on the PATH: it finds it from
  `where git`.
- On Windows both installers clone to `%LOCALAPPDATA%\unio\agent`, but
  `install.sh` and `install.ps1`'s plugin mode keep using a clone already at
  `~/.local/share/unio/agent`, since a Codex config names it.
- `bin/unio.cmd` stays ASCII with CRLF line ends: cmd reads it in the
  console's code page, and can miss a label in a file with bare LFs. The
  help's accents reach it only as data, under code page 65001.
- `install.ps1` stays ASCII, with accents as `\u` escapes that its `Say`
  turns back: Windows PowerShell 5.1 reads a file without a BOM in the
  system's code page, and a BOM breaks `[scriptblock]::Create`. It runs in
  the person's own session, so it never calls `exit`.
