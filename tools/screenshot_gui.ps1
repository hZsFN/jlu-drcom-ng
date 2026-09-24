# Capture the DrCOM window into a PNG (works even when it is not focused).
# Usage: powershell -File tools/screenshot_gui.ps1 [-Out <path>]

param(
    [string]$Out = (Join-Path $env:TEMP 'drcom-gui.png')
)

Add-Type -AssemblyName System.Windows.Forms,System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinCap {
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
    public struct RECT { public int L, T, R, B; }
}
"@

$proc = Get-Process flet -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } |
    Select-Object -First 1

if (-not $proc) { Write-Host 'no DrCOM window found'; exit 1 }

$handle = $proc.MainWindowHandle
$rect = New-Object WinCap+RECT
[WinCap]::GetWindowRect($handle, [ref]$rect) | Out-Null
$width = $rect.R - $rect.L
$height = $rect.B - $rect.T

$bitmap = New-Object System.Drawing.Bitmap($width, $height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$hdc = $graphics.GetHdc()
# PW_RENDERFULLCONTENT (2) is required for GPU-composited (Flutter) windows.
$ok = [WinCap]::PrintWindow($handle, $hdc, 2)
$graphics.ReleaseHdc($hdc)
$graphics.Dispose()

if (-not $ok) {
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen($rect.L, $rect.T, 0, 0, $bitmap.Size)
    $graphics.Dispose()
}

$bitmap.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bitmap.Dispose()
Write-Host ("saved {0} ({1}x{2})" -f $Out, $width, $height)
