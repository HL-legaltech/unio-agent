# Security

## Report a vulnerability

Report it privately from this repository's Security tab (Report a
vulnerability). Do not open a public issue or pull request about it.

## A token leaked

A Unio agent token (`unio_` followed by 46 letters and digits) acts in its
workspace as Unio Bot. If one appears where it should not, such as a chat, a
commit, a log or a screenshot:

1. Revoke it in Unio, in Configurações > Pessoal > Agente de código. An
   administrator can also revoke anyone's token in Configurações > Sistema >
   Agentes.
2. Generate a new one there and store it with the command the page shows.

Deleting the file or the message does not revoke the token.

## What the plugin does with a token

The token stays in `~/.config/unio/` with mode 600. The plugin's scripts
(`plugins/unio/scripts/`) send it only to `https://api.unio.so`, in an
`Authorization` header. They never print it, never pass it on a command line,
and never copy it anywhere else.

## Releases and rollback

Merging to `main` is a release. To roll one back, merge a revert and raise
`version` above the bad release (a higher PATCH), so every client sees a newer
version and installs it. Clients with auto-update off keep the bad version
until they update with `/unio:update`. Unio's server can also raise the minimum
version it accepts, so that it refuses a release that must not keep running.
