# Build a complete Kikitori release:
#   engine (PyInstaller)  -> build\engine\KikitoriEngine\
#   app    (WinUI 3)      -> build\app\
#   assembled             -> dist\Kikitori\   (Kikitori.exe + engine\KikitoriEngine.exe)
#   optional zip          -> dist\Kikitori-<version>-win-x64.zip
#
# Usage (from anywhere):
#   powershell -ExecutionPolicy Bypass -File build.ps1                 # full build
#   powershell -ExecutionPolicy Bypass -File build.ps1 -SkipEngine     # reuse existing engine build
#   powershell -ExecutionPolicy Bypass -File build.ps1 -Zip            # also create the portable zip
#   powershell -ExecutionPolicy Bypass -File build.ps1 -Installer      # also create the setup exe (Inno Setup 6)
#
# Requirements: .venv (repo root) with engine\requirements.txt + engine\requirements-build.txt, .NET 8 SDK,
#               Inno Setup 6 for -Installer (winget install JRSoftware.InnoSetup).
# ASCII-only on purpose (Windows PowerShell 5.1 misreads UTF-8 scripts without BOM).
param(
    [switch]$SkipEngine,
    [switch]$SkipApp,
    [switch]$Zip,
    [switch]$Installer
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Run-Script([string]$rel) {
    $path = Join-Path $root $rel
    Write-Host "== $rel" -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File $path
    if ($LASTEXITCODE -ne 0) { throw "$rel failed (exit $LASTEXITCODE)" }
}

if (-not $SkipEngine) { Run-Script "engine\packaging\build_engine.ps1" }
if (-not $SkipApp) { Run-Script "app\tools\publish.ps1" }

$engineDir = Join-Path $root "build\engine\KikitoriEngine"
$appDir = Join-Path $root "build\app"
if (-not (Test-Path (Join-Path $engineDir "KikitoriEngine.exe"))) { throw "engine build missing: $engineDir" }
if (-not (Test-Path (Join-Path $appDir "Kikitori.exe"))) { throw "app build missing: $appDir" }

# ---- assemble dist\Kikitori ----
$dist = Join-Path $root "dist\Kikitori"
Write-Host "== assemble $dist" -ForegroundColor Cyan
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
New-Item -ItemType Directory -Force $dist | Out-Null

# robocopy exit codes 0-7 mean success
robocopy $appDir $dist /E /XD (Join-Path $appDir "engine") /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copy app failed ($LASTEXITCODE)" }
robocopy $engineDir (Join-Path $dist "engine") /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copy engine failed ($LASTEXITCODE)" }
$global:LASTEXITCODE = 0

Copy-Item (Join-Path $root "LICENSE") (Join-Path $dist "LICENSE.txt")
$notices = Join-Path $root "THIRD-PARTY-NOTICES.md"
if (Test-Path $notices) { Copy-Item $notices (Join-Path $dist "THIRD-PARTY-NOTICES.md") }

$files = Get-ChildItem $dist -Recurse -File
"assembled: {0} files, {1:N2} GB" -f $files.Count, (($files | Measure-Object Length -Sum).Sum / 1GB)

$ver = (Get-Item (Join-Path $dist "Kikitori.exe")).VersionInfo.ProductVersion
if (-not $ver) { $ver = "0.0.0" }
$ver = ($ver -split '\+')[0]
"version: $ver"

# ---- optional zip (tar.exe = bsdtar, supports zip64; Compress-Archive in PS 5.1 chokes on big files) ----
if ($Zip) {
    $zipPath = Join-Path $root "dist\Kikitori-$ver-win-x64.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Write-Host "== zip $zipPath" -ForegroundColor Cyan
    & tar.exe -a -c -f $zipPath -C (Join-Path $root "dist") "Kikitori"
    if ($LASTEXITCODE -ne 0) { throw "zip failed" }
    "zip: {0:N2} GB" -f ((Get-Item $zipPath).Length / 1GB)
}

# ---- optional installer (Inno Setup 6) ----
if ($Installer) {
    $iscc = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($cmd) { $iscc = $cmd.Source }
    }
    if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
    Write-Host "== installer ($iscc)" -ForegroundColor Cyan
    & $iscc /Q "/DAppVersion=$ver" "/DSourceDir=$dist" "/DOutputDir=$(Join-Path $root 'dist')" (Join-Path $root "installer\installer.iss")
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
    $setup = Join-Path $root "dist\Kikitori-$ver-setup.exe"
    "installer: {0} ({1:N2} GB)" -f $setup, ((Get-Item $setup).Length / 1GB)
}
