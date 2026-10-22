# AGENTS.md

Rules for people and coding agents working in this repository. The repository
is public: every file, commit and pull request here is visible to anyone.

## What this is

- `.claude-plugin/marketplace.json`: the `unio` marketplace, with one plugin.
- `plugins/unio/`: the `unio` plugin for Claude Code. Its skill
  (`skills/unio/SKILL.md`) and its header helper (`scripts/mcp-headers.sh`)
  also serve Codex from a clone of this repository.
- `tests/`: the plugin's scripts, run against a fake Unio API in a throwaway
  HOME.

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

## Commits

Conventional Commits, in English: `type(scope): subject`, imperative, lower
case, no final period, with a body wrapped at 72 columns that says what was
wrong, what changed and why. The last line is the task trailer, `Unio: <KEY>`.
No AI attribution. Stage named paths only.

## Secrets and names

- Never commit a token, an environment value, or a real `~/.config/unio` file.
  Tests use the fake token in `tests/fake_api.py`.
- No hostnames other than `unio.so` and `api.unio.so`, and no person's or
  customer's name, except the code owner in `.github/CODEOWNERS`.

## Traps

- Scripts are POSIX `sh`: they run under dash, bash, macOS `sh` and Git Bash,
  with `curl`, `sed`, `awk`, `tr`, `grep` and `find` as the only tools.
- A token never reaches a process's argv. It leaves a script only through the
  `printf` builtin, and `curl` reads it from stdin with `-K -`.
- A script that a command skill injects with `` !`…` `` must exit 0, or Claude
  Code aborts the command. Its `allowed-tools` rule must match the injected
  line exactly, or the command stops with "Shell command permission check
  failed" in the default permission mode.
- The session hook's `additionalContext` carries fixed text only, never a name
  or CHANGELOG text: what someone types in Unio must not reach Claude's context
  through the hook.
- Text a person reads at runtime (hook messages, command output) is Brazilian
  Portuguese: sentence case, no em dash, no emoji. Repository files are in
  English.
