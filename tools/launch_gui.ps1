# Open the graphical client (detached, so it keeps running after this call).
#
# Usage: powershell -File tools/launch_gui.ps1 [-DataDir <path>]

param(
    [string]$DataDir = (Join-Path $env:TEMP 'drcom-gui')
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python).Source

$proc = Start-Process -FilePath $python `
    -ArgumentList @('main.py', '--data-dir', $DataDir, '--no-single-instance') `
    -WorkingDirectory $repoRoot `
    -PassThru

Write-Host ("GUI launched: pid={0}  dataDir={1}" -f $proc.Id, $DataDir)
