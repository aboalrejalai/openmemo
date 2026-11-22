# OpenMemo one-command installer for Windows (PowerShell 5.1+).
#
#   irm https://raw.githubusercontent.com/aboalrejalai/openmemo/main/install.ps1 | iex
#
# Clones (or updates) OpenMemo into $env:OPENMEMO_HOME (default ~\openmemo), adds
# its plugin\bin folder to your user PATH, then runs `memo init --yes`.
$ErrorActionPreference = 'Stop'

$Repo = if ($env:OPENMEMO_REPO) { $env:OPENMEMO_REPO } else { 'https://github.com/aboalrejalai/openmemo.git' }
$Dir  = if ($env:OPENMEMO_HOME) { $env:OPENMEMO_HOME } else { Join-Path $HOME 'openmemo' }

function Say($m) { Write-Host "==> $m" -ForegroundColor Green }
function Die($m) { Write-Host "error: $m" -ForegroundColor Red; exit 1 }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die 'git is required: winget install Git.Git' }

$Py = $null
foreach ($c in @(@('py', '-3'), @('python'))) {
  if (Get-Command $c[0] -ErrorAction SilentlyContinue) {
    $args0 = @($c | Select-Object -Skip 1)
    & $c[0] @args0 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>$null
    if ($LASTEXITCODE -eq 0) { $Py = $c; break }
  }
}
if (-not $Py) { Die 'Python 3.9 or newer is required: winget install Python.Python.3.12' }
$PyArgs = @($Py | Select-Object -Skip 1)
& $Py[0] @PyArgs -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")' 2>$null
if ($LASTEXITCODE -ne 0) { Die 'your Python has no SQLite FTS5 support; install Python from python.org' }

if (Test-Path (Join-Path $Dir '.git')) {
  Say "Updating OpenMemo in $Dir"
  git -C $Dir pull --ff-only --quiet
  if ($LASTEXITCODE -ne 0) { Die "could not update $Dir (local changes?). Run: git -C `"$Dir`" status" }
} elseif (Test-Path $Dir) {
  Die "$Dir exists and is not an OpenMemo checkout. Set OPENMEMO_HOME to another folder."
} else {
  Say "Downloading OpenMemo into $Dir"
  git clone --quiet $Repo $Dir
  if ($LASTEXITCODE -ne 0) { Die "git clone failed" }
}

$Bin = Join-Path $Dir 'plugin\bin'
$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not (($UserPath -split ';') -contains $Bin)) {
  [Environment]::SetEnvironmentVariable('Path', (($UserPath, $Bin) -ne '' -join ';').Trim(';'), 'User')
  Say "Added $Bin to your user PATH (open a new terminal to use memo)"
}
$env:Path = "$Bin;$env:Path"

Say 'Wiring your AI agents (memo init)'
$env:MEMO_VAULT = $Dir
& (Join-Path $Bin 'memo.cmd') init --yes
$ver = & (Join-Path $Bin 'memo.cmd') --version
Write-Host ''
Say "$ver is ready."
Write-Host '  memo ui       open the Studio at http://127.0.0.1:8766'
Write-Host '  memo doctor   check the installation'
