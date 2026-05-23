# Brainchild bootstrap installer — Windows PowerShell.
# Usage:  irm https://brainchild.sh/install.ps1 | iex

$ErrorActionPreference = "Stop"

$RepoUrl    = if ($env:BRAINCHILD_REPO) { $env:BRAINCHILD_REPO } else { "https://github.com/Unioedtech/brainchild.git" }
$InstallDir = Join-Path $HOME ".brainchild"
$RepoDir    = Join-Path $InstallDir "repo"

function Say($m)  { Write-Host "  $m" }
function Ok($m)   { Write-Host "  $([char]0x2713) $m" -ForegroundColor Green }
function Err($m)  { Write-Host "  $([char]0x2717) $m" -ForegroundColor Red }
function Bail($m) { Err $m; exit 1 }

Write-Host ""
Write-Host "────────────────────────────────────────────────────────────"
Write-Host "  Brainchild bootstrap (Windows)"
Write-Host "────────────────────────────────────────────────────────────"
Write-Host ""

Ok "OS: windows"

# Python check
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Bail "Python 3.8+ not found. Install from https://python.org (check 'Add to PATH')." }
$ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Ok "Python: $ver"
& python -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)"
if ($LASTEXITCODE -ne 0) { Bail "Python 3.8+ required (found $ver)" }

# claude check
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) { Bail "Claude Code not found and npm not installed. Install Node from https://nodejs.org then run again." }
    Say "Claude Code not found. Install via npm? [Y/n]"
    $a = Read-Host
    if ($a -eq "n" -or $a -eq "N") { Bail "Install Claude Code first: npm i -g @anthropic-ai/claude-code" }
    & npm i -g "@anthropic-ai/claude-code"
    if ($LASTEXITCODE -ne 0) { Bail "npm install failed" }
}
Ok "claude found"

# git check
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) { Bail "git not found. Install from https://git-scm.com/download/win" }

# Clone / update
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if (Test-Path (Join-Path $RepoDir ".git")) {
    Say "updating existing checkout…"
    & git -C $RepoDir fetch --quiet origin
    & git -C $RepoDir reset --hard --quiet origin/HEAD
} else {
    Say "cloning $RepoUrl"
    & git clone --quiet $RepoUrl $RepoDir
    if ($LASTEXITCODE -ne 0) { Bail "git clone failed (set BRAINCHILD_REPO env var to override)" }
}

# pip install
Say "installing Python deps (user-local)…"
& python -m pip install --quiet --user --upgrade pip
& python -m pip install --quiet --user --force-reinstall --no-cache-dir --no-deps $RepoDir
if ($LASTEXITCODE -ne 0) { Bail "pip install (brainchild) failed" }
& python -m pip install --quiet --user "keyring>=24" "imageio-ffmpeg>=0.4.9" "pypdf>=4.0" "python-docx>=1.1"
if ($LASTEXITCODE -ne 0) { Bail "pip install (deps) failed" }
Ok "Python package installed"

# Hand off to wizard
Ok "bootstrap complete — launching wizard"
Write-Host ""
& python -m brainchild install
