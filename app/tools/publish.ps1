# Publish Kikitori (WinUI 3, unpackaged, self-contained win-x64) to <repo>\build\app
#   powershell -ExecutionPolicy Bypass -File app\tools\publish.ps1
# Needs only the .NET 8 SDK (no Visual Studio, no Windows SDK install). ASCII-only script.
$ErrorActionPreference = "Stop"
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$proj = Join-Path $repo "app\Kikitori\Kikitori.csproj"
$out = Join-Path $repo "build\app"

if (Test-Path $out) {
    # keep an engine folder if someone already copied it next to the app
    Get-ChildItem $out -Force | Where-Object { $_.Name -ne "engine" } | Remove-Item -Recurse -Force
}
dotnet publish $proj -c Release -r win-x64 --self-contained true -p:Platform=x64 -o $out
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed" }

$exe = Join-Path $out "Kikitori.exe"
if (-not (Test-Path $exe)) { throw "Kikitori.exe missing in $out" }
$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum
"published: $exe"
"total size: {0:N1} MB ({1} files)" -f ($size / 1MB), (Get-ChildItem $out -Recurse -File).Count
