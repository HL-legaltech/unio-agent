"""Runs the real Claude Code with the plugin against tests/e2e/fake_services.py.

Proves, on whatever system runs it, what only a real session shows: that the
SessionStart hook runs and its context reaches the model, that the scripts
/unio:doctor and /unio:connect inject are allowed to run, and that the MCP
header helper sends each folder its own workspace's token, and no token at all
where its workspace has none.

Three folders under a throwaway HOME:
  "Proj Alpha"  mapped to org_Alpha in ~/.config/unio/projects, written in the
                form the installer writes (`pwd -P` of the shell that runs the
                plugin's scripts), with a space and capitals in its name;
  beta          no mapping, so the default workspace, org_Beta;
  gamma         mapped to org_Gamma, which has no token.

On Windows the PATH Claude Code gets keeps Git's cmd folder but drops its
bin and usr\\bin folders, as a default Git for Windows install leaves it, so
nothing passes only because the runner put sh.exe on the PATH.

Usage: python -I tests/e2e/claude_session.py [--claude <path>] [--keep]
Exit status 0 when every check passes; each failure is printed.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PLUGIN = os.path.join(REPO, "plugins", "unio")
WINDOWS = os.name == "nt"


def fake_token(seed):
    return "unio_" + (seed * 4)[:46]


# Prepended to each script in the test copy: one line per run, naming what
# the script was given (folder, process, variables) but never a value that
# could hold a token.
TRACER = (
    '[ -z "${UNIO_E2E_TRACE:-}" ] || { printf "%s args=%s pwd=%s project=%s claude_pid=%s ppid=%s os=%s winpid=%s vars=%s\\n" '
    '"@NAME@" "$*" "$(pwd)" "${CLAUDE_PROJECT_DIR-<unset>}" "${CLAUDE_PID-<unset>}" "$PPID" '
    '"$(uname -s 2>/dev/null)" "$(cat /proc/$$/winpid 2>/dev/null)" '
    '"$(env | sed -n \'s/^\\(CLAUDE[A-Z_]*\\)=.*/\\1/p\' | sort | tr \'\\n\' \' \')" >> "$UNIO_E2E_TRACE"; } 2>/dev/null'
)

TOKEN_A = fake_token("AlphaFakeTokenNotASecret")
TOKEN_B = fake_token("BetaFakeTokenNotASecret")
FAILURES = []


def check(ok, what, detail=""):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        FAILURES.append(what)
        if detail:
            print("     " + detail.replace("\n", "\n     "))


def write(path, text, private=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as out:
        out.write(text)
    if private and not WINDOWS:
        os.chmod(path, 0o600)


def posix_shell():
    """The sh the plugin's scripts run under: Git's on Windows."""
    if WINDOWS:
        git = shutil.which("git")
        if git:
            root = os.path.dirname(os.path.dirname(os.path.realpath(git)))
            for candidate in ("bin\\sh.exe", "usr\\bin\\sh.exe"):
                path = os.path.join(root, candidate)
                if os.path.exists(path):
                    return path
        sys.exit("Git for Windows' sh.exe was not found next to git.exe")
    return shutil.which("sh") or "/bin/sh"


def shell_path(sh, folder, env):
    """The folder as `pwd -P` names it in that shell: what install.sh maps."""
    out = subprocess.run([sh, "-c", 'cd "$1" && pwd -P', "sh", folder],
                         capture_output=True, text=True, env=env, check=True)
    return out.stdout.strip()


def windows_path_without_git_tools(path):
    keep = []
    for entry in path.split(os.pathsep):
        low = entry.lower().rstrip("\\/")
        if "\\git\\" in low + "\\" and not low.endswith("\\cmd"):
            continue
        keep.append(entry)
    return os.pathsep.join(keep)


def read_log(path, start):
    try:
        with open(path, encoding="utf-8") as lines:
            entries = [json.loads(line) for line in lines if line.strip()]
    except FileNotFoundError:
        entries = []
    return entries[start:], len(entries)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude", default=shutil.which("claude"))
    parser.add_argument("--keep", action="store_true", help="keep the throwaway folder")
    args = parser.parse_args()
    if not args.claude:
        sys.exit("claude was not found; pass --claude")

    root = tempfile.mkdtemp(prefix="unio-e2e-")
    home = os.path.join(root, "home")
    config = os.path.join(root, "claude-config")
    log_file = os.path.join(root, "requests.jsonl")
    port_file = os.path.join(root, "port")
    folders = {name: os.path.join(home, "work", name) for name in ("Proj Alpha", "beta", "gamma")}
    for folder in folders.values():
        os.makedirs(folder)
    os.makedirs(config)

    server = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "fake_services.py"),
                               port_file, log_file, TOKEN_A + "=org_Alpha", TOKEN_B + "=org_Beta"])
    try:
        for _ in range(100):
            if os.path.exists(port_file):
                break
            time.sleep(0.1)
        with open(port_file, encoding="utf-8") as f:
            port = f.read().strip()
        base = "http://127.0.0.1:" + port

        # The plugin as installed, but pointed at the fake MCP server.
        plugin = os.path.join(root, "plugin", "unio")
        shutil.copytree(PLUGIN, plugin)
        mcp_path = os.path.join(plugin, ".mcp.json")
        with open(mcp_path, encoding="utf-8") as f:
            mcp = json.load(f)
        mcp["mcpServers"]["unio"]["url"] = base + "/v1/mcp"
        write(mcp_path, json.dumps(mcp, indent=2) + "\n")
        trace = os.path.join(root, "trace.log")
        for name in ("session-start.sh", "mcp-headers.sh", "doctor.sh"):
            path = os.path.join(plugin, "scripts", name)
            with open(path, encoding="utf-8") as f:
                lines = f.read().split("\n")
            lines.insert(1, TRACER.replace("@NAME@", name))
            write(path, "\n".join(lines))

        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ANTHROPIC_", "CLAUDE", "UNIO_"))}
        env.update({
            "HOME": home,
            "CLAUDE_CONFIG_DIR": config,
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_API_KEY": "fake-key-for-tests",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "UNIO_E2E_TRACE": trace,
        })
        if WINDOWS:
            env["PATH"] = windows_path_without_git_tools(env.get("PATH", ""))

        sh = posix_shell()
        conf = os.path.join(home, ".config", "unio")
        write(os.path.join(conf, "api-url"), base + "\n")
        write(os.path.join(conf, "workspaces", "org_Alpha", "token"), TOKEN_A, private=True)
        write(os.path.join(conf, "workspaces", "org_Beta", "token"), TOKEN_B, private=True)
        write(os.path.join(conf, "default-workspace"), "org_Beta\n")
        shell_env = dict(env, HOME=home)
        alpha = shell_path(sh, folders["Proj Alpha"], shell_env)
        gamma = shell_path(sh, folders["gamma"], shell_env)
        write(os.path.join(conf, "projects"), "%s\torg_Alpha\n%s\torg_Gamma\n" % (alpha, gamma))
        print("mapped %s and %s" % (alpha, gamma))

        offset = 0

        def run(folder, prompt):
            nonlocal offset
            try:
                urllib.request.urlopen(urllib.request.Request(base + "/control/expect-mcp", data=b"", method="POST"))
            except urllib.error.HTTPError as refused:
                check(False, "the fake API waits for this session's MCP connection",
                      refused.read().decode("utf-8", "replace"))
            started = time.time()
            done = subprocess.run(
                [args.claude, "-p", "--plugin-dir", plugin, "--output-format", "json", prompt],
                cwd=folder, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=240, stdin=subprocess.DEVNULL)
            # The refresh hook runs in the background; give it a moment.
            time.sleep(2)
            entries, offset = read_log(log_file, offset)
            print("  ran %r in %s: exit %d in %.0fs, %d requests"
                  % (prompt, os.path.basename(folder), done.returncode, time.time() - started, len(entries)))
            # The fake lets the first message go after 30 s without MCP, and
            # some checks below hold vacuously when no MCP request came.
            paths = [e.get("path") for e in entries]
            first = paths.index("/v1/messages") if "/v1/messages" in paths else len(paths)
            check("/v1/mcp" in paths[:first],
                  "%s in %s: Claude Code reached the MCP server before its first message"
                  % (prompt, os.path.basename(folder)), "requests: %s" % paths)
            return done, entries

        def texts(entries):
            return "\n".join(e.get("text", "") for e in entries if e.get("path") == "/v1/messages")

        def mcp_tokens(entries):
            return {e.get("token") for e in entries if e.get("path") == "/v1/mcp"}

        # 1. A folder mapped to its own workspace: /unio:doctor and the helper.
        done, entries = run(folders["Proj Alpha"], "/unio:doctor")
        check(done.returncode == 0, "alpha: claude -p exits 0",
              (done.stderr or done.stdout)[-1500:])
        report = texts(entries)
        check("core token:" in report, "alpha: /unio:doctor's report reached the model",
              report[-1500:] or (done.stderr or done.stdout)[-1500:])
        check("org_Alpha" in report and "ligado a esta pasta" in report,
              "alpha: the doctor names the folder's own workspace", report[-800:])
        check("core api: token aceito" in report, "alpha: the doctor's API check passed", report[-800:])
        check("ok core folder:" in report, "alpha: the doctor confirms the connection sees this folder",
              report[-800:])
        tokens = mcp_tokens(entries)
        check(tokens == {TOKEN_A}, "alpha: every MCP request carried org_Alpha's token",
              "tokens seen: %s" % sorted(t[:12] if t else "<none>" for t in tokens))
        clients = {e.get("client") for e in entries if e.get("path") == "/v1/mcp"}
        check(all(c and c.startswith("claude-code/") for c in clients),
              "alpha: MCP requests named the plugin's version", "clients seen: %s" % sorted(map(str, clients)))
        seen = glob.glob(os.path.join(config, "plugins", "data", "*", "seen-version"))
        check(bool(seen), "alpha: the SessionStart hook ran (seen-version written)",
              "plugin data: %s" % glob.glob(os.path.join(config, "plugins", "data", "*", "*")))

        # 2. A folder with no mapping uses the default workspace.
        done, entries = run(folders["beta"], "Responda ok.")
        check(done.returncode == 0, "beta: claude -p exits 0", (done.stderr or done.stdout)[-1500:])
        tokens = mcp_tokens(entries)
        check(tokens == {TOKEN_B}, "beta: every MCP request carried the default workspace's token",
              "tokens seen: %s" % sorted(t[:12] if t else "<none>" for t in tokens))

        # 3. A folder whose workspace has no token: no token at all, and the
        #    hook tells the model so.
        done, entries = run(folders["gamma"], "Responda ok.")
        check(done.returncode == 0, "gamma: claude -p exits 0", (done.stderr or done.stdout)[-1500:])
        tokens = mcp_tokens(entries)
        check(not (tokens & {TOKEN_A, TOKEN_B}), "gamma: no other workspace's token was sent",
              "tokens seen: %s" % sorted(t[:12] if t else "<none>" for t in tokens))
        check("The Unio tools have no token on this computer" in texts(entries),
              "gamma: the hook's context reached the model", texts(entries)[-800:])

        # 4. /unio:connect's injected script is allowed in the default mode.
        done, entries = run(folders["Proj Alpha"], "/unio:connect")
        check(done.returncode == 0, "connect: claude -p exits 0", (done.stderr or done.stdout)[-1500:])
        check("Para conectar este computador ao Unio" in texts(entries),
              "connect: the instructions reached the model",
              texts(entries)[-800:] or (done.stderr or done.stdout)[-1500:])
        try:
            with open(trace, encoding="utf-8") as f:
                print("trace:\n  " + f.read().strip().replace("\n", "\n  "))
        except FileNotFoundError:
            check(False, "the plugin's scripts left a trace", "no script ran at all")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        if args.keep:
            print("kept " + root)
        else:
            shutil.rmtree(root, ignore_errors=True)

    if FAILURES:
        print("%d check(s) failed" % len(FAILURES))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
