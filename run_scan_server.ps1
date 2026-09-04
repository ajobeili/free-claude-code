# FCC Trader Workbench launcher.
# Credentials live in `.env` at the repo root (see `.env.example`).
# Copy .env.example to .env and fill in your CAPITAL_* values, then run this script.

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

Write-Host "Starting scan_server from $repoRoot"
& $python -m tools.scan_server
