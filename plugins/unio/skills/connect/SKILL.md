---
name: connect
description: Connect this computer to a Unio workspace. Shows where the token comes from and the terminal command that stores it, so the token never enters the chat.
disable-model-invocation: true
allowed-tools: Bash(sh "${CLAUDE_PLUGIN_ROOT}/scripts/connect.sh" instructions)
---

# Connect to Unio

## Instructions for the user

!`sh "${CLAUDE_PLUGIN_ROOT}/scripts/connect.sh" instructions`

## What to do

1. Show the user the instructions above as printed; they are in Portuguese, and so is your answer. Keep the command on a line of its own in a code block, exactly as printed: the user copies it into another terminal.
2. Do not run that command, and never read, write or list anything under `~/.config/unio`. The command needs the user's own terminal to read the token without showing it, and nothing in this conversation may see the token.
3. Never ask the user to paste a token into this chat. If one appears in the conversation anyway, tell them to revoke it in Configurações > Sistema > Agentes and issue a new one, because the transcript now holds it.
4. When the user says the command saved the token, tell them to run `/mcp`, choose `plugin:unio:unio` and Reconnect (or run `/reload-plugins`), then `/unio:doctor` to confirm.
