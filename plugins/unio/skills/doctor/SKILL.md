---
name: doctor
description: Check this computer's Unio setup (plugin, token, API, updates, leftovers of a hand-made setup, Codex) and explain how to fix what fails.
disable-model-invocation: true
allowed-tools: Bash(sh "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh" claude-code)
---

# Unio doctor

## Report

!`sh "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh" claude-code`

## What to do

Summarize the report for the user, in Portuguese: one line saying whether Unio works on this computer, then each `bad` line and each `warn` line, most important first, each with its fix as the report gives it. Leave out `ok` and `info` lines unless the user asks for them.

- `core` lines decide whether Unio works. A token or api line is fixed with `/unio:connect` (after a new token from Configurações > Sistema > Agentes); a version line with `/unio:update`, then `/reload-plugins`.
- `migration` lines are leftovers from a setup made by hand. Offer to fix them, but each one changes files outside this project, so ask before you act, and do one at a time:
  - Move `~/.claude/skills/unio` to `~/.local/share/unio/retired-skill`; never delete it.
  - Save the user-scope server entry before removing it: with `umask 077`, write `jq '.mcpServers.unio' ~/.claude.json` to `~/.local/share/unio/retired-mcp.json`, then run `claude mcp remove unio -s user`. Never run `claude mcp get`, and never print the saved entry: it may hold a token.
  - In `~/.claude/settings.json`, copy each `mcp__unio__` permission rule to the same rule with `mcp__plugin_unio_unio__`, and widen a hook matcher `mcp__unio__.*` to `mcp__(plugin_unio_)?unio__.*`, but only when the scripts that hook runs accept both tool prefixes.
- `codex` lines only advise; point at the Codex section of this plugin's README (https://github.com/HL-legaltech/unio-agent#codex).

Never ask for, print or read out a token, and never open the token files.
