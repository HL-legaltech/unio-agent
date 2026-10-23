# Unio agent

The Unio plugin for Claude Code, and the same skill and connection for Codex: it lets a coding agent track its work in [Unio](https://unio.so) as the workspace's agent, through Unio's MCP server at `https://api.unio.so/v1/mcp`.

The plugin carries four things:

- **The Unio skill**, the protocol the agent follows: which sessions to track, how to shape a task, when to comment, how to follow a pull request until it is live, and when to plan a project.
- **The Unio MCP server**, connected with the token stored on your computer. The plugin sends its version with every connection, so Unio can tell you when to update.
- **A notice when a session starts**, when Unio is not connected, the token was refused or expires soon, or a new release is out.
- **Four commands:** `/unio:connect`, `/unio:doctor`, `/unio:update` and `/unio:help`.

## What you need

- A Unio workspace with agents turned on, where you have the developer role. You generate your own token in Unio, under Configurações > Pessoal > Agente de código (<https://unio.so/app/settings/coding-agent>).
- Claude Code 2.1.292 or later (`claude --version`), or Codex.
- Linux, WSL or macOS, which bring the `sh`, `curl` and `ps` the plugin uses (and `lsof`, on macOS). On Windows, [Git for Windows](https://git-scm.com/downloads/win): Claude Code needs git there to install a plugin from GitHub, and Git for Windows brings Git Bash, where Claude Code runs the plugin's scripts. Run the commands below in a Git Bash terminal.

<a id="install"></a>

## Install with one command

In a terminal of your own, outside Claude Code (on Windows, Git Bash), run:

```sh
curl -fsSL https://raw.githubusercontent.com/HL-legaltech/unio-agent/main/install.sh | sh
```

It installs the plugin, then prints a link to Unio and a code, and opens your browser when it can. Sign in, check that the page shows the same code, pick the workspace and approve. The token goes from Unio straight into `~/.config/unio/workspaces/<workspace>/token`: it never appears on screen, in a clipboard, in the chat or in the shell history. Over SSH, open the link on any device. `--browser` asks for this flow explicitly, for example with `--renew`.

Or, in Unio, open Configurações > Pessoal > Agente de código (<https://unio.so/app/settings/coding-agent>), generate a token, and paste the command the page shows:

```sh
curl -fsSL https://raw.githubusercontent.com/HL-legaltech/unio-agent/main/install.sh | sh -s -- --workspace org_…
```

The page fills in `--workspace`, the workspace the token belongs to. `--client codex` sets up Codex instead of Claude Code, and `--renew` only replaces the token, installing nothing. With `--workspace`, the installer:

1. checks that `claude` is Claude Code 2.1.292 or later;
2. installs the plugin from this marketplace, unless it is installed already;
3. turns on auto-update, when your settings hold the marketplace entry exactly as Claude Code wrote it, and otherwise says how;
4. reads the token at a hidden prompt, so it never appears on screen, in the chat or in the shell history, and checks its shape (`unio_` followed by 46 letters and digits);
5. asks Unio whether it accepts the token, and tells a network failure apart from a refused token and from another workspace's token;
6. only then saves it to `~/.config/unio/workspaces/<workspace>/token`, readable by you alone.

The first workspace on a computer serves every folder. For another one, it asks whether that workspace serves only the folder you ran it in (the default) or the whole computer, except the folders tied to another workspace. Each line it prints is in Portuguese, and the last one is `Conectado ao Unio.` when everything worked.

To read the script before running it, open [install.sh](install.sh). It runs the same from a downloaded copy: `sh install.sh --workspace org_…`.

## Install in Claude Code by hand

```sh
claude plugin marketplace add HL-legaltech/unio-agent
claude plugin install unio@unio
```

Or in one command:

```sh
claude plugin install unio --marketplace HL-legaltech/unio-agent
```

In a session that is already open, run `/reload-plugins`. On a computer without a GitHub SSH key, set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1` in your shell profile first, so adding and updating the marketplace use HTTPS.

## Connect

[The one command](#install) installs and connects at once. To connect again, for example when a token expires, run `/unio:connect` in Claude Code. It prints `sh <plugin folder>/scripts/connect.sh browser`, to run in another terminal, outside Claude Code, from your project's folder. That command asks Unio for a token and waits up to 10 minutes while you approve the request in the browser: check that the page shows the code your terminal shows, pick the workspace and approve. Approve only a request you just started; a link or code someone sends you would connect their computer as you. The token is saved as the installer saves it. If this computer already uses another workspace, it asks whether the new one covers only this folder or the whole computer.

`/unio:connect` also sends you to Configurações > Pessoal > Agente de código for a token to paste, and when the folder already has a workspace, it prints the installer's command with `--renew` for it. Otherwise, run the command the page shows.

Then run `/mcp`, choose `plugin:unio:unio` and Reconnect (or run `/reload-plugins`), and `/unio:doctor` to confirm. Never paste a token into the chat: anything you type there stays in the transcript. If one ends up there, revoke it in Configurações > Pessoal > Agente de código and generate a new one.

For a setup made by hand, `/unio:connect` also prints `sh <plugin folder>/scripts/connect.sh` when the folder has no workspace yet. That command makes the same checks as the installer and saves the token to `~/.config/unio/token`. It also takes the token from a pipe, for example from a password manager.

### Allow Unio's tools

Claude Code asks before each call to an MCP tool. To let the agent use Unio without asking each time, add this rule to `permissions.allow` in `~/.claude/settings.json`:

```json
"mcp__plugin_unio_unio__*"
```

## Turn on auto-update

Claude Code does not update plugins from this marketplace until you turn it on: in Claude Code, open `/plugin`, go to Marketplaces, choose `unio` and select Enable auto-update. From then on, Claude Code installs each release in the background and shows `Plugin updated: unio · Run /reload-plugins to apply`. The first session on a new version says what changed.

An administrator can turn it on for everyone, and install the plugin with it, in Claude Code's managed settings:

```json
{
  "extraKnownMarketplaces": {
    "unio": {
      "source": { "source": "github", "repo": "HL-legaltech/unio-agent" },
      "autoUpdate": true
    }
  },
  "enabledPlugins": { "unio@unio": true }
}
```

Where managed settings restrict marketplaces with `strictKnownMarketplaces`, add `{ "source": "github", "repo": "HL-legaltech/unio-agent" }` to that list too.

<a id="update"></a>

## Update now

Without auto-update, a notice at the start of a session tells you once when a release is out, and the agent tells you when Unio reports your version behind. To update:

- **In Claude Code:** run `/unio:update`, then `/reload-plugins`.
- **From a shell:**

  ```sh
  claude plugin marketplace update unio
  claude plugin update unio@unio
  ```

  Then run `/reload-plugins` in each open session. New sessions load the new version on their own.
- **In Codex:** run `git -C ~/.local/share/unio/agent pull --ff-only`, then start a new Codex session.

When a version is below the minimum Unio accepts, every Unio tool but `get_me` refuses with `agent_client_outdated` until you update.

## Commands

| Command | What it does |
| --- | --- |
| `/unio:connect` | Shows the terminal command that gets a token you approve in the browser, and the page-and-installer alternative. |
| `/unio:doctor` | Checks the plugin, Claude Code, the token, Unio's answer, updates, Codex, and leftovers of a setup made by hand, and says how to fix each problem. It never shows the token. |
| `/unio:update` | Installs the newest release now. |
| `/unio:help` | What the plugin does, its commands and ready prompts, and what this release changed. |

## Ready prompts

Type them as ordinary messages, in either language; the skill recognizes them.

| Intent | English | Português |
| --- | --- | --- |
| Track a task | Track this in Unio: \<link or key\> | Acompanhe isto no Unio: \<link ou chave\> |
| New task | Create a Unio task for this work | Crie uma tarefa no Unio para este trabalho |
| Follow-up | File a follow-up in Unio for \<topic\> | Registre um desdobramento no Unio sobre \<assunto\> |
| Project | Plan this as a Unio project | Planeje isto como um projeto no Unio |
| Needs | Put what \<KEY\> needs from \<name\> at the top | Coloque no topo o que \<chave\> precisa de \<nome\> |
| Update | Post a Unio project update for \<project\>: \<health\> | Publique uma atualização do projeto \<projeto\> no Unio: \<saúde\> |
| Opt out | Don't track this session in Unio | Não acompanhe esta sessão no Unio |
| Version | Is my Unio skill up to date? | Minha skill do Unio está atualizada? |

## Where things live

- **Tokens:** `~/.config/unio/workspaces/<workspace id>/token`, mode 600, one per workspace. The id starts with `org_` and is the `ws=` value of a Unio link. Claude Code, Codex and the commands all read these files.
- **Which workspace a folder uses:** `~/.config/unio/projects` maps a folder to a workspace, one `<folder><TAB><workspace id>` line each, and the longest match wins. Every other folder uses the workspace named in `~/.config/unio/default-workspace`. The installer writes both.
- **A setup from before the installer:** `~/.config/unio/token`, read only while `default-workspace` does not exist. The hand-setup command (`connect.sh`) still saves a token there, and refuses to while `default-workspace` exists.
- **What the session notice remembers:** status codes, version numbers and days left, in the plugin's data folder. Never a name or a token.
- **On Windows,** `~` is your profile folder (`%USERPROFILE%`), so the tokens are in `%USERPROFILE%\.config\unio\workspaces`. Git Bash names the folders in `projects` `/c/Users/...`; a line in the `C:\Users\...` or `C:/Users/...` form works too, and on Windows and macOS case does not matter.

### How a session finds its folder

Claude Code connects to Unio through the plugin's header helper, which it starts in the plugin's own folder and tells nothing about the project. So when any folder is tied to a workspace, the helper asks the coding agent itself: on Linux and macOS it reads the working folder of the Claude Code (or Codex) process that started it. Windows lets no program read another's folder, so there the session hook writes Claude Code's folder to `~/.local/state/unio/sessions/<process id>` as the session starts, and the helper reads it.

If the helper cannot learn the folder, it connects without a token rather than with the computer's default one, which could belong to another workspace, and `/unio:doctor` says why. Without any folder in `projects`, every folder uses the default and nothing is looked up. Codex on Windows has no session hook, so there it uses the default workspace only.

<a id="codex"></a>

## Codex

Codex uses the same skill, the same header helper and the same token files, from a clone of this repository. The installer with `--client codex` does steps 1 and 3 below and prints the block of step 2 with your home folder in it; add that block yourself. By hand:

1. Clone it, and link the skill where Codex looks for skills:

   ```sh
   git clone https://github.com/HL-legaltech/unio-agent.git ~/.local/share/unio/agent
   mkdir -p ~/.agents/skills
   ln -s ~/.local/share/unio/agent/plugins/unio/skills/unio ~/.agents/skills/unio
   ```

   On Windows, Git Bash's `ln -s` copies the folder instead, and the copy never updates; make a junction instead: `cmd //c mklink /J "%USERPROFILE%\.agents\skills\unio" "%USERPROFILE%\.local\share\unio\agent\plugins\unio\skills\unio"`.

2. Add the server to `~/.codex/config.toml`. Remove any `[mcp_servers.unio]` block already there first. Codex splits the helper line into words but does not run it through a shell, so it would not expand `~` or `$HOME`; this command writes your real home folder into it:

   ```sh
   cat >> ~/.codex/config.toml <<EOF

   [mcp_servers.unio]
   url = "https://api.unio.so/v1/mcp"
   http_headers_helper = "sh '$HOME/.local/share/unio/agent/plugins/unio/scripts/mcp-headers.sh' codex"
   EOF
   ```

   On Windows, Codex finds no `sh` on its PATH: name Git's own, and write both paths in the `C:/` form, for example `http_headers_helper = "'C:/Program Files/Git/usr/bin/sh.exe' 'C:/Users/you/.local/share/unio/agent/plugins/unio/scripts/mcp-headers.sh' codex"`. The installer prints that line with your own paths.

   Codex runs the helper on every connection to Unio. It reads the token file itself and sends `X-Unio-Client: codex/<version>`, so the token never sits in an environment variable that every program you start inherits.

3. Store the token, in a terminal:

   ```sh
   sh ~/.local/share/unio/agent/plugins/unio/scripts/connect.sh
   ```

4. Check the setup:

   ```sh
   sh ~/.local/share/unio/agent/plugins/unio/scripts/doctor.sh codex
   ```

If Codex read the token from an exported variable before (`bearer_token_env_var`), remove that export from your shell profile once Codex reaches Unio through the helper. To update, see [Update now](#update).

## Move a setup made by hand

If you wired Unio into Claude Code by hand, with a skill in `~/.claude/skills/unio`, a `unio` MCP server in your user configuration, or permission rules and hooks that name `mcp__unio__` tools, move to the plugin in this order. Each step leaves exactly one connection to Unio.

1. **Install the plugin** as above. While your own server entry exists, Claude Code treats the plugin's server, which has the same URL, as a duplicate and keeps using yours.
2. **Retire your skill,** so only the plugin's loads:

   ```sh
   mkdir -p ~/.local/share/unio
   mv ~/.claude/skills/unio ~/.local/share/unio/retired-skill
   ```

3. **Save your server entry, then remove it.** The saved file can hold a token, so it is written readable by you alone. Do not use `claude mcp get unio` for this: it prints the entry's headers, token included.

   ```sh
   (umask 077 && jq '.mcpServers.unio' ~/.claude.json > ~/.local/share/unio/retired-mcp.json)
   claude mcp remove unio -s user
   ```

   From the next session on, the plugin's server connects with the same token file.
4. **Rename your rules.** The plugin's tools are `mcp__plugin_unio_unio__<tool>`, not `mcp__unio__<tool>`. In `~/.claude/settings.json`, copy each `mcp__unio__` permission rule to the same rule with `mcp__plugin_unio_unio__`, and widen each hook matcher `mcp__unio__.*` to `mcp__(plugin_unio_)?unio__.*`, once the scripts those hooks run accept both tool names.
5. **Keep headless runners on their own setup.** A runner that passes its own MCP configuration keeps `--strict-mcp-config`, which keeps the plugin's server out, and adds `"enabledPlugins": {"unio@unio": false}` to the settings it passes with `--settings`, which also keeps the plugin's skill out.
6. **Turn on auto-update** as above, then run `/unio:doctor`: it shows no `migration` lines, and `/mcp` lists `plugin:unio:unio`.

To go back: `claude plugin uninstall unio@unio`, move `~/.local/share/unio/retired-skill` back to `~/.claude/skills/unio`, and run `claude mcp add-json unio --scope user "$(cat ~/.local/share/unio/retired-mcp.json)"`. If the saved entry holds a token in a fixed `Authorization` header, issue a new token instead of restoring that one.

## Uninstall

```sh
claude plugin uninstall unio@unio
claude plugin marketplace remove unio
```

For Codex, remove the `[mcp_servers.unio]` block, `~/.agents/skills/unio` and `~/.local/share/unio/agent`. Deleting a token file in `~/.config/unio` does not revoke the token: revoke it in Unio, under Configurações > Pessoal > Agente de código.

## Security

The token stays in `~/.config/unio/` with mode 600 and goes only to `https://api.unio.so`. The scripts never print it, never put it on a command line, and never copy it anywhere else. To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Contributing

Every change is a pull request; see [AGENTS.md](AGENTS.md) for the release rules and the checks to run.

Copyright HL-legaltech. All rights reserved.
