# Installs Unio's answering service on Windows: the program that answers for
# a workspace's agent from this computer, with its Claude Code. It is the
# command the Agentes page in Unio shows, for Windows PowerShell 5.1 and
# PowerShell 7:
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/HL-legaltech/unio-agent/main/install.ps1))) -Runner -Workspace org_<id>
#
# It checks Git, Python 3.9 or later and Claude Code, clones this repository
# to %LOCALAPPDATA%\unio\agent (or updates the clone there), then runs
# runner\unio-runner setup, which reads the service's token at a hidden
# prompt and installs a Task Scheduler task. macOS and Linux use install.sh.
#
# It runs inside the person's own session, so it never calls exit, which
# would close their window. The file is ASCII only: Windows PowerShell 5.1
# reads a file without a BOM in the system's code page, and a BOM breaks
# [scriptblock]::Create. Say turns the \u escapes back into accents.

param(
    [switch]$Runner,
    [string]$Workspace
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

function Install-Runner {
    if (-not $Runner) {
        Say 'Este instalador instala o servi\u00e7o de respostas do Unio no Windows. Rode o comando que a p\u00e1gina Agentes mostra, que termina em -Runner -Workspace org_...'
        return
    }
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
    $rest = @($python | Select-Object -Skip 1)
    & $python[0] @rest $setup setup --workspace $Workspace
}

Install-Runner
