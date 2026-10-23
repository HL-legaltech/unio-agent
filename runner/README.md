# Answering service

The answering service (`unio-inbox`) answers for a workspace's agent: when someone mentions the agent in a task or project comment, replies to it, or assigns it a task, the service runs a headless Claude Code session on the computer it is installed on and posts the answer in Unio. `unio runner` sets it up as a service of that computer and manages it afterwards.

It runs on the computer of a person in the workspace, with that person's Claude Code, and counts against their Claude plan. Each workspace that wants answers runs its own. It is not part of the Claude Code plugin in [`plugins/unio`](../plugins/unio).

## What you need

- macOS, Linux with systemd, or Windows 10 or 11.
- Python 3.9 or later, and Git.
- Claude Code, signed in to the account whose plan pays for the answers (`claude auth status` says so).
- The service's token. A developer who administers the workspace generates it in Unio, under Configurações > Sistema > Agentes.
- Optionally the GitHub CLI (`gh`), signed in with `gh auth login`, for the service to read private repositories. Without it, it reads public ones only.

## Install

The Agentes page shows the command with your workspace filled in. On macOS and Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/HL-legaltech/unio-agent/main/install.sh | sh -s -- --runner --workspace org_…
```

On Windows, in PowerShell:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/HL-legaltech/unio-agent/main/install.ps1))) -Runner -Workspace org_…
```

Both clone this repository (to `~/.local/share/unio/agent`, or `%LOCALAPPDATA%\unio\agent` on Windows), or update the clone already there, put the `unio` command on your PATH, and run `unio runner setup`, which:

1. checks Python, Git, Claude Code and its sign-in, and whether `gh` is there;
2. reads the token at a hidden prompt, so it never appears on screen or in the shell history;
3. asks `unio-inbox --check` whether Unio accepts it for this workspace, and says why not (a revoked token, agents switched off, another workspace's token, or a coding agent token instead of the service's), saving nothing then;
4. asks, for each team of the agent, which GitHub repositories (`owner/name`, separated by commas) the service may read a copy of;
5. installs and starts the service for this workspace;
6. waits up to 60 seconds for the service's first start and ends with `Serviço de respostas ligado para <workspace>.`

Every line it prints is in Portuguese. To read the scripts first, open [install.sh](../install.sh), [install.ps1](../install.ps1), [bin/unio](../bin/unio) and [unio-runner](unio-runner).

## Commands

The service's commands are `unio runner <command>`. `unio` is [`bin/unio`](../bin/unio) in the clone (`bin\unio.cmd` on Windows): the installer links it as `~/.local/bin/unio` on macOS and Linux, and puts the clone's `bin` folder on your user `Path` on Windows. It never replaces a `~/.local/bin/unio` that is something else, and says when `~/.local/bin` is not on your `PATH`; the clone's own `~/.local/share/unio/agent/bin/unio` works either way. `unio help` lists the commands, and `unio --version` prints the runner's version.

| Command | What it does |
| --- | --- |
| `setup` | Everything above. `--api URL` points the service at another Unio API, `--token-stdin` reads the token from stdin, `--repos-json FILE` takes the repositories from a file (`{"ENG": ["owner/name"]}`), `--no-service` prepares the profile without installing the service, and `--yes` keeps the saved token and repositories without asking. Run it again to change the repositories or the token. |
| `status [--json]` | For each workspace on this computer: whether the service runs, the version it started with, whether it updates itself, and the last line of its log. `--json` prints one object, `{"workspaces": [{"workspace", "state", "version", "autoUpdate", "token", "lastLog"}]}`, where `state` is `running`, `stopped`, `failed`, `absent` (no service installed) or `unknown`. |
| `start`, `stop`, `restart` | Start, stop or restart the service. A stopped service stays stopped, across logins and restarts, until `start`. |
| `logs [-n N] [-f]` | The last `N` lines of the service's log (40 by default), and with `-f` each new one as it comes. `--lines N` and `--follow` are the long forms. |
| `uninstall [--yes]` | Stops and removes the service and deletes the token from this computer, once you confirm it (`--yes` confirms ahead). The token stays valid in Unio until you revoke it on the Agentes page. |
| `update` | Pulls the clone (`git pull --ff-only`). A running service that updates itself moves to a newer version on its own once it finishes what it is doing, so no answer is cut off; a service with auto-update off, or one that failed, is restarted. |

Every command but `update` takes `--workspace org_…`. It can be left out while this computer holds the token of one workspace only; with none or several, the command names them and stops. `status` without it shows every workspace.

Run without a terminal, as from a script, a command never asks anything: it stops and names the option that answers the question, such as `--token-stdin`, `--repos-json` or `--yes`. Each command exits with 0 when it worked, 1 on a problem and 2 on a mistake in the command or its options.

## Where things live

| | macOS and Linux | Windows |
| --- | --- | --- |
| The clone | `~/.local/share/unio/agent` | `%LOCALAPPDATA%\unio\agent` |
| The `unio` command | `~/.local/bin/unio`, a link to the clone's `bin/unio` | the clone's `bin\unio.cmd`, its folder on the user `Path` |
| A workspace's settings | `~/.config/unio/runners/<workspace>/` | `%APPDATA%\unio\runners\<workspace>\` |
| Its log and work files | `~/.local/state/unio/runners/<workspace>/` | `%LOCALAPPDATA%\unio\runners\<workspace>\` |

`XDG_CONFIG_HOME` and `XDG_STATE_HOME` move the last two on macOS and Linux; setup writes the ones it ran with into the service's definition, so the service reads the same folders. `unio-inbox --workspace <id> --print-paths` prints the exact folders; `unio-runner` asks it rather than working them out itself.

The settings folder holds `runner-token` (readable by you alone), `repos.json` (team identifier to repositories), `runner-id` (the service's identity on the agent's page), and optionally `env`, `KEY=VALUE` lines the service reads at start (a variable set in the service's own environment wins). `responder.md`, `reviewer.md` or `needs.md` there replace the protocol texts in [`protocol/`](protocol) for that workspace.

## The service on each system

- **Linux:** a systemd user service, `unio-runner@<workspace>.service`, from the template `~/.config/systemd/user/unio-runner@.service`. It restarts 10 seconds after any exit, has the `PATH` setup ran with, and is capped at 1 GB of memory. Answers wait for free memory only when `UNIO_INBOX_MEMORY_FLOOR_GIB` in the settings folder's `env` sets a floor, in GiB available. Without lingering, systemd stops user services when you log out; setup prints the `loginctl enable-linger` command when it is off. Its own output goes to `journalctl --user -u unio-runner@<workspace>`.
- **macOS:** a LaunchAgent, `~/Library/LaunchAgents/so.unio.runner.<workspace>.plist`, loaded in your login session and kept alive, with the `PATH` setup ran with. Its own output goes to `service.log` in the log folder. Run setup from a Terminal in your own session, not over SSH, so launchd can load it.
- **Windows:** a Task Scheduler task, `Unio Runner <workspace>` at the root of the library (where a standard user may add one), that runs `pythonw.exe` (no window) while you are logged on, with no time limit and on battery too. It starts at each logon and, from setup on, is started again every minute; a second copy is never started, so after any exit, including a crash or a self-update, the service is back within a minute without waiting for the next logon. Python runs in UTF-8 mode (`-X utf8`).

The definitions come from the templates in [`services/`](services), and setup writes them again each time it runs. It never touches a `unio-inbox.service` left by a setup made by hand.

## Updates

The service keeps its clone up to date: at start and every 6 hours, between answers, it fetches `main` and, when `runner/VERSION` there is newer and the clone has no local changes, fast-forwards and exits so the service manager starts the new version. It does the same, between answers, as soon as the clone holds a newer version, after `unio runner update` or another workspace's service moved it. `UNIO_RUNNER_AUTOUPDATE=off`, in the environment or the settings folder's `env`, turns that off; `unio runner update` then updates by hand.

## Development

```sh
python3 -m unittest discover -s runner/tests -v
```

The tests use the standard library only and touch no real service manager, Claude Code or network. `unio-runner` always runs the real `unio-inbox` beside it; `tests/runner_support.py` puts a fake `claude`, `systemctl`, `loginctl`, `launchctl`, `plutil`, `schtasks` and `powershell` on `PATH` in a throwaway home, and the fake service managers start the real `unio-inbox` in the background and stop it again. A fake Unio API on 127.0.0.1 answers both scripts: the one in `runner_support.py` for setup, and the one in `tests/inbox_support.py`, with a fake `claude` that answers a mention, for the runner's own tests. They also run `bin/unio` through `sh`, and `bin\unio.cmd` on Windows. CI runs them on Linux (Python 3.9 and 3.12), macOS and Windows, and also checks the generated definitions with `systemd-analyze verify`, `plutil -lint` and `schtasks /Create`.
