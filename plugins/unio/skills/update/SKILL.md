---
name: update
description: Update the Unio plugin to its latest release now, instead of waiting for auto-update.
disable-model-invocation: true
allowed-tools:
  - Bash(claude plugin marketplace update unio)
  - Bash(claude plugin update unio@unio)
---

# Update the Unio plugin

This plugin runs from `${CLAUDE_PLUGIN_ROOT}`. If that folder is not inside a `plugins/cache` folder, it was loaded with `--plugin-dir`: tell the user that updates do not apply to it, and stop.

Answer the user in Portuguese.

1. Run `claude plugin marketplace update unio`, then `claude plugin update unio@unio`.
2. If the second command says the plugin is already at the latest version, tell the user and stop.
3. Otherwise tell the user the new version is installed and that `/reload-plugins` applies it to this session; new sessions load it on their own, and the first one says what changed.
4. If either command fails, show its last line and suggest `/unio:doctor`.
5. Suggest turning on auto-update so this happens by itself: `/plugin` > Marketplaces > unio > Enable auto-update.
