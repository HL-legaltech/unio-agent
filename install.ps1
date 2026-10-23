# Installs Unio on Windows, for Windows PowerShell 5.1 and PowerShell 7. It is
# the command the Agente de codigo page in Unio shows, which installs the
# Claude Code plugin (or, with -Client codex, sets Codex up) and connects the
# computer to a workspace:
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/HL-legaltech/unio-agent/main/install.ps1))) [-Workspace org_<id>] [-Client claude-code|codex] [-Renew] [-Browser]
#
# Those flags mean what install.sh's do, and install.sh does the work: this
# script checks Git for Windows and Claude Code, keeps the clone, puts the
# unio command on the user's Path, then runs install.sh from the clone with
# Git's own sh.exe, so the token and folder rules live in one script.
#
# With -Runner it installs Unio's answering service instead: the program that
# answers for a workspace's agent from this computer, with its Claude Code.
# It is the command the Agentes page shows:
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/HL-legaltech/unio-agent/main/install.ps1))) -Runner -Workspace org_<id>
#
# It checks Git, Python 3.9 or later and Claude Code, clones this repository
# to %LOCALAPPDATA%\unio\agent (or updates the clone there), puts the clone's
# bin folder on the user's Path for the unio command, then runs
# runner\unio-runner setup (`unio runner setup`), which reads the service's
# token at a hidden prompt and installs a Task Scheduler task. macOS and
# Linux use install.sh.
#
# It runs inside the person's own session, so it never calls exit, which
# would close their window. The file is ASCII only: Windows PowerShell 5.1
# reads a file without a BOM in the system's code page, and a BOM breaks
# [scriptblock]::Create. Say turns the \u escapes back into accents.

param(
    [switch]$Runner,
    [string]$Workspace,
    [string]$Client = 'claude-code',
    [switch]$Renew,
    [switch]$Browser
)

$Repository = 'HL-legaltech/unio-agent'

function Say([string]$Text, [object[]]$Values = @()) {
    $message = [regex]::Unescape($Text)
    if ($Values.Count -gt 0) { $message = $message -f $Values }
    Write-Host $message
}

# The last line a failed command printed.
function Get-LastLine($Output) {
    $lines = @($Output | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
    if ($lines.Count -eq 0) { return '' }
    $line = $lines[$lines.Count - 1]
    if ($line.Length -gt 200) { $line = $line.Substring(0, 200) }
    return $line
}

# The command and arguments that run Python 3.9 or later, or $null. The
# python.exe that Windows ships is a Microsoft Store alias that only prints a
# hint, so each candidate must answer the version check itself.
function Find-Python {
    foreach ($candidate in @(@('py', '-3'), @('python'))) {
        if (-not (Get-Command $candidate[0] -CommandType Application -ErrorAction SilentlyContinue)) { continue }
        $rest = @($candidate | Select-Object -Skip 1)
        $answer = & $candidate[0] @rest -c 'import sys; print(int(sys.version_info >= (3, 9)))' 2>$null
        if ($LASTEXITCODE -eq 0 -and "$answer".Trim() -eq '1') { return , $candidate }
    }
    return $null
}

# Clones this repository to $Clone, or updates the clone already there.
function Get-Clone([string]$Clone) {
    $prompt = $env:GIT_TERMINAL_PROMPT
    $env:GIT_TERMINAL_PROMPT = '0'
    try {
        $skill = Join-Path $Clone 'plugins\unio\skills\unio\SKILL.md'
        if ((Test-Path -LiteralPath (Join-Path $Clone '.git')) -and (Test-Path -LiteralPath $skill)) {
            git -C $Clone pull --quiet --ff-only 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Say 'Unio atualizado em {0}.' $Clone
            } else {
                Say 'N\u00e3o foi poss\u00edvel atualizar {0} com git pull --ff-only; a vers\u00e3o que j\u00e1 est\u00e1 l\u00e1 continua valendo.' $Clone
            }
            return $true
        }
        if (Test-Path -LiteralPath $Clone) {
            Say '{0} j\u00e1 existe e n\u00e3o \u00e9 um clone do Unio. Mova essa pasta e rode este comando de novo.' $Clone
            return $false
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Clone) | Out-Null
        $output = git clone --quiet "https://github.com/$Repository.git" $Clone 2>&1
        if ($LASTEXITCODE -ne 0) {
            $why = Get-LastLine $output
            if ($why) { $why = " ($why)" }
            Say 'N\u00e3o foi poss\u00edvel baixar o Unio em {0}{1}. Rode este comando de novo em instantes.' $Clone, $why
            return $false
        }
        Say 'Unio baixado em {0}.' $Clone
        return $true
    } finally {
        $env:GIT_TERMINAL_PROMPT = $prompt
    }
}

# Tells running programs, Explorer above all, that the user's environment
# changed, so a terminal opened next gets the new Path without signing out.
function Send-EnvironmentChange {
    if (-not ('UnioInstall.Native' -as [type])) {
        Add-Type -Namespace UnioInstall -Name Native -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);
'@
    }
    $result = [UIntPtr]::Zero
    # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG, at most 5 s.
    [void][UnioInstall.Native]::SendMessageTimeout([IntPtr]0xffff, 0x1a, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result)
}

# Puts $Folder on the user's Path, which needs no administrator, and on this
# session's; $true when the user's Path did not have it yet. The value is
# read and written raw: it is often REG_EXPAND_SZ with %VARIABLES% in it,
# which [Environment]::SetEnvironmentVariable would expand and save as REG_SZ.
function Add-UserPath([string]$Folder) {
    $want = $Folder.TrimEnd('\')
    $present = $false
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        foreach ($entry in $raw -split ';') {
            if ([Environment]::ExpandEnvironmentVariables($entry).TrimEnd('\') -ieq $want) { $present = $true }
        }
        if (-not $present) {
            $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
            if ($null -ne $key.GetValue('Path')) { $kind = $key.GetValueKind('Path') }
            $value = $raw.TrimEnd(';')
            if ($value) { $value += ';' }
            $key.SetValue('Path', $value + $Folder, $kind)
        }
    } finally {
        $key.Close()
    }
    if (-not (@("$env:Path" -split ';') | Where-Object { $_.TrimEnd('\') -ieq $want })) {
        $env:Path = "$env:Path".TrimEnd(';') + ';' + $Folder
    }
    if ($present) { return $false }
    Send-EnvironmentChange
    return $true
}

# The sh.exe of Git for Windows, from git.exe's own folder: Git's cmd folder
# is on the Path, its bin folder is not. $null without Git for Windows.
function Find-GitSh {
    $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $git) { return $null }
    $gitRoot = Split-Path -Parent (Split-Path -Parent $git.Source)
    foreach ($relative in @('bin\sh.exe', 'usr\bin\sh.exe')) {
        $candidate = Join-Path $gitRoot $relative
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

# Puts the clone's bin folder, home of unio.cmd, on the user's Path.
function Set-UnioCommand([string]$Clone) {
    $bin = Join-Path $Clone 'bin'
    if (-not (Test-Path -LiteralPath (Join-Path $bin 'unio.cmd') -PathType Leaf)) { return }
    try {
        if (Add-UserPath $bin) {
            Say 'Pasta {0} acrescentada ao Path do usu\u00e1rio. Abra um terminal novo para usar o comando unio; neste ele j\u00e1 funciona.' $bin
        } else {
            Say 'O comando unio j\u00e1 est\u00e1 no Path do usu\u00e1rio.'
        }
    } catch {
        Say 'N\u00e3o foi poss\u00edvel acrescentar {0} ao Path do usu\u00e1rio ({1}). O comando do Unio est\u00e1 em {2}.' $bin, $_.Exception.Message, (Join-Path $bin 'unio.cmd')
    }
}

# The plugin, or Codex, and a workspace's token, through install.sh.
function Install-Plugin {
    if ($Client -cne 'claude-code' -and $Client -cne 'codex') {
        Say '-Client aceita claude-code ou codex.'
        return
    }
    if ($Workspace -and $Workspace -cnotmatch '^org_[A-Za-z0-9]+$') {
        Say 'O valor de -Workspace n\u00e3o \u00e9 um workspace do Unio (org_ seguido de letras e n\u00fameros). Copie de novo o comando da p\u00e1gina Agente de c\u00f3digo.'
        return
    }
    if ($Renew -and -not $Workspace) {
        Say '-Renew precisa de -Workspace org_...: copie o comando da p\u00e1gina Agente de c\u00f3digo.'
        return
    }
    if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) {
        Say 'Este instalador \u00e9 para o Windows. No macOS e no Linux, rode o comando com install.sh que a p\u00e1gina Agente de c\u00f3digo mostra.'
        return
    }
    if ($env:CLAUDECODE) {
        Say 'Rode este comando em outro terminal, fora do Claude Code: aqui, o que voc\u00ea digita pode entrar na conversa.'
        return
    }
    if (-not $env:LOCALAPPDATA) {
        Say 'A vari\u00e1vel LOCALAPPDATA n\u00e3o est\u00e1 definida, ent\u00e3o n\u00e3o h\u00e1 onde instalar. Nada foi instalado.'
        return
    }
    $sh = Find-GitSh
    if (-not $sh) {
        Say 'O Unio precisa do Git for Windows: o Claude Code instala plugins com o git, e os scripts do Unio rodam no Git Bash que ele traz. Instale com winget install --id Git.Git -e, abra outro terminal e rode este comando de novo.'
        return
    }
    if ($Client -ceq 'claude-code' -and -not $Renew -and -not (Get-Command claude -ErrorAction SilentlyContinue)) {
        Say 'O comando claude n\u00e3o est\u00e1 no PATH. Instale o Claude Code (irm https://claude.ai/install.ps1 | iex), abra outro terminal e rode este comando de novo.'
        return
    }
    # install.sh keeps using a clone already where it cloned before, since a
    # Codex config names its helper there, so the unio command must be that
    # clone's. Git's sh takes HOME from USERPROFILE when HOME is not set.
    $clone = Join-Path $env:LOCALAPPDATA 'unio\agent'
    $profileDir = if ($env:HOME) { $env:HOME } else { $env:USERPROFILE }
    if ($profileDir) {
        $legacy = Join-Path $profileDir '.local\share\unio\agent'
        if (Test-Path -LiteralPath (Join-Path $legacy '.git') -PathType Container) { $clone = $legacy }
    }
    if (-not (Get-Clone $clone)) { return }
    Set-UnioCommand $clone
    $arguments = @('--client', $Client)
    if ($Workspace) { $arguments += @('--workspace', $Workspace) }
    if ($Renew) { $arguments += '--renew' }
    if ($Browser) { $arguments += '--browser' }
    # Forward slashes: Git's sh reads C:/... as a path, and C:\... as escapes.
    & $sh (($clone -replace '\\', '/') + '/install.sh') @arguments
}

function Install-Runner {
    if ($Workspace -cnotmatch '^org_[A-Za-z0-9]+$') {
        Say 'O valor de -Workspace n\u00e3o \u00e9 um workspace do Unio (org_ seguido de letras e n\u00fameros). Copie de novo o comando da p\u00e1gina Agentes.'
        return
    }
    if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) {
        Say 'Este instalador \u00e9 para o Windows. No macOS e no Linux, rode o comando com install.sh que a p\u00e1gina Agentes mostra.'
        return
    }
    if ($env:CLAUDECODE) {
        Say 'Rode este comando em outro terminal, fora do Claude Code: aqui, o que voc\u00ea digita pode entrar na conversa.'
        return
    }
    if (-not $env:LOCALAPPDATA) {
        Say 'A vari\u00e1vel LOCALAPPDATA n\u00e3o est\u00e1 definida, ent\u00e3o n\u00e3o h\u00e1 onde instalar. Nada foi instalado.'
        return
    }
    if (-not (Get-Command git -CommandType Application -ErrorAction SilentlyContinue)) {
        Say 'O comando git n\u00e3o est\u00e1 no PATH. Instale o Git (winget install Git.Git), abra outro terminal e rode este comando de novo.'
        return
    }
    $python = Find-Python
    if (-not $python) {
        Say 'O Python 3.9 ou mais novo n\u00e3o foi encontrado. Instale com winget install Python.Python.3.12, abra outro terminal e rode este comando de novo.'
        return
    }
    if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
        Say 'O comando claude n\u00e3o est\u00e1 no PATH. Instale o Claude Code, entre na sua conta e rode este comando de novo.'
        return
    }
    $clone = Join-Path $env:LOCALAPPDATA 'unio\agent'
    if (-not (Get-Clone $clone)) { return }
    $setup = Join-Path $clone 'runner\unio-runner'
    if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
        Say 'O clone em {0} ainda n\u00e3o tem o servi\u00e7o de respostas. Rode este comando de novo em instantes.' $clone
        return
    }
    Set-UnioCommand $clone
    $rest = @($python | Select-Object -Skip 1)
    & $python[0] @rest $setup setup --workspace $Workspace
}

# Dot-sourced, it only defines its functions, as CI does to test them.
if ($MyInvocation.InvocationName -ne '.') {
    if ($Runner) { Install-Runner } else { Install-Plugin }
}
