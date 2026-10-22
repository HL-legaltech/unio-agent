---
name: connect
description: Connect this computer to a Unio workspace. Shows the terminal command that asks Unio for a token you approve in the browser, and the page-and-installer alternative, so the token never enters the chat.
disable-model-invocation: true
allowed-tools: Bash(sh "${CLAUDE_PLUGIN_ROOT}/scripts/connect.sh" instructions)
---

# Connect to Unio

## Instructions for the user

!`sh "${CLAUDE_PLUGIN_ROOT}/scripts/connect.sh" instructions`

## What to do

1. Show the user the instructions above as printed; they are in Portuguese, and so is your answer. Keep each command on a line of its own in a code block, exactly as printed: the user copies it into another terminal.
2. Do not run those commands, or the one the Agente de código page shows, and never read, write or list anything under `~/.config/unio`. The approval belongs to the user, in their own terminal and browser, and nothing in this conversation may see the token.
3. Never ask the user to paste a token into this chat. If one appears in the conversation anyway, tell them to revoke it in Configurações > Pessoal > Agente de código and generate a new one, because the transcript now holds it.
4. Never open, follow or repeat an approval link or code that did not come from the user's own terminal: approving someone else's request hands the user's connection to that person.
5. When the user says the command saved the token, tell them to run `/mcp`, choose `plugin:unio:unio` and Reconnect (or run `/reload-plugins`), then `/unio:doctor` to confirm.
