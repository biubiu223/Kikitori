# Render docs\social-preview\social-preview.html -> docs\images\social-preview.png (1280x640)
# using Microsoft Edge in headless mode (preinstalled on Windows 10/11).
#   powershell -ExecutionPolicy Bypass -File docs\social-preview\render.ps1
# ASCII-only on purpose (Windows PowerShell 5.1).
$ErrorActionPreference = "Stop"
$html = Join-Path $PSScriptRoot "social-preview.html"
$out = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\images\social-preview.png"))
$edge = @(
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { throw "Microsoft Edge not found" }

# separate profile so it never touches the user's running Edge
$profileDir = Join-Path $env:TEMP "kikitori-social-preview-edge"
$url = "file:///" + ($html -replace '\\', '/')
if (Test-Path $out) { Remove-Item $out -Force }
& $edge --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 `
    --user-data-dir="$profileDir" --window-size=1280,640 --screenshot="$out" $url | Out-Null
Start-Sleep -Milliseconds 500
if (-not (Test-Path $out)) { throw "screenshot failed" }

Add-Type -AssemblyName System.Drawing
$img = [Drawing.Image]::FromFile($out)
"rendered: $out ({0}x{1}, {2:N0} KB)" -f $img.Width, $img.Height, ((Get-Item $out).Length / 1KB)
$img.Dispose()
