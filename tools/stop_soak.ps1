# Stop a previously started soak run (see start_soak.ps1).
#
# Matches on the command line so it only ever touches our own CLI process and
# never the DSH web server or any other Python program on the machine.

$ErrorActionPreference = 'Stop'

$targets = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*--cli*login*' }

if (-not $targets) {
    Write-Host 'no soak process found'
    exit 0
}

foreach ($proc in $targets) {
    try {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
        Write-Host ("stopped pid={0}" -f $proc.ProcessId)
    } catch {
        Write-Host ("could not stop pid={0}: {1}" -f $proc.ProcessId, $_.Exception.Message)
    }
}
