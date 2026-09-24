# Launch a long soak run as a process that outlives the calling shell.
#
# The harness reaps child processes when a bash/PS call returns, so plain
# `nohup ... &` does not survive.  Start-Process creates an independent process.
#
# Usage:  powershell -File tools/start_soak.ps1 -DataDir <path>

param(
    [string]$DataDir = (Join-Path $env:TEMP 'drcom-soak'),
    [int]$ApiPort = 8861
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python).Source

$proc = Start-Process -FilePath $python `
    -ArgumentList @('main.py', '--cli', 'login', '--data-dir', $DataDir, '--log-level', 'INFO') `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -PassThru

Write-Host ("soak started: pid={0} dataDir={1}" -f $proc.Id, $DataDir)
Write-Host ("query it with: curl http://127.0.0.1:{0}/status" -f $ApiPort)
