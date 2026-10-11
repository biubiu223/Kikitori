# 一键打包引擎：PyInstaller onedir → <仓库>\build\engine\KikitoriEngine\KikitoriEngine.exe
#
# 用法（在任意目录；一般由仓库根目录的 build.ps1 调用）：
#   powershell -ExecutionPolicy Bypass -File engine\packaging\build_engine.ps1
#   powershell -ExecutionPolicy Bypass -File engine\packaging\build_engine.ps1 -Python D:\path\to\python.exe
#   powershell -ExecutionPolicy Bypass -File engine\packaging\build_engine.ps1 -BundleCuda     完整构建（开发用）
#
# 默认是精简构建：不带 NVIDIA 运行库（约 2GB），首次用显卡时由引擎下载（见 docs/protocol.md 第 7 节）。
# -BundleCuda：把 .venv 里的 NVIDIA DLL 一起打包，不需要下载。
# 默认用仓库根目录下 .venv 里的 Python（需要已安装 engine\requirements.txt 和 engine\requirements-build.txt）。
# 中间文件放在 <仓库>\build\pyinstaller-work\，都在 build\ 下，可以整个忽略/删除。
# 显卡运行库清单 engine\packaging\cuda_manifest.json 由 engine\packaging\make_cuda_manifest.py 生成（升级 CUDA 包后重新生成）。
param(
    [string]$Python = "",
    [switch]$BundleCuda,
    [switch]$SkipSmokeTest
)
$ErrorActionPreference = "Stop"

$EngineDir = Split-Path -Parent $PSScriptRoot      # engine\（engine_server.py 所在目录）
$Root = Split-Path -Parent $EngineDir              # 仓库根目录（.venv、build\ 所在目录）
if (-not $Python) { $Python = Join-Path $Root ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) {
    throw "找不到 Python：$Python（先在仓库根目录创建 .venv 并安装 engine\requirements.txt，或用 -Python 指定）"
}

$Spec = Join-Path $PSScriptRoot "engine.spec"
$Dist = Join-Path $Root "build\engine"
$Work = Join-Path $Root "build\pyinstaller-work"
$Out  = Join-Path $Dist "KikitoriEngine"

Write-Host "== 检查 PyInstaller"
& $Python -c "import PyInstaller, sys; print('PyInstaller', PyInstaller.__version__, '/ Python', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) {
    Write-Host "== 安装 PyInstaller"
    & $Python -m pip install "pyinstaller>=6.16"
    if ($LASTEXITCODE -ne 0) { throw "安装 PyInstaller 失败" }
}

if ($BundleCuda) {
    Write-Host "== 打包（完整构建：带 NVIDIA 运行库，2GB 多）"
    $env:KIKITORI_BUNDLE_CUDA = "1"
} else {
    Write-Host "== 打包（精简构建：不带 NVIDIA 运行库）"
    $env:KIKITORI_BUNDLE_CUDA = "0"
}
$env:PYTHONDONTWRITEBYTECODE = "1"
& $Python -m PyInstaller --noconfirm --clean --log-level WARN --distpath $Dist --workpath $Work $Spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

$Exe = Join-Path $Out "KikitoriEngine.exe"
if (-not (Test-Path $Exe)) { throw "没有生成 $Exe" }
$size = (Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("== 完成：{0}（共 {1:N2} GB）" -f $Exe, ($size / 1GB))

if (-not $SkipSmokeTest) {
    # 冒烟测试：启动 → 收到 hello / idle → 关闭 stdin → 进程自己退出
    # （PowerShell 5.1 传给外部程序的参数里不能有双引号，所以这段 Python 只用单引号）
    Write-Host "== 冒烟测试"
    $code = @'
import json, subprocess, sys, time
p = subprocess.Popen([sys.argv[1]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     creationflags=0x08000000)
lines = [json.loads(p.stdout.readline()) for _ in range(2)]
assert lines[0]['event'] == 'hello' and lines[1].get('state') == 'idle', lines
p.stdin.close(); t = time.time(); p.wait(5)
print('hello/idle OK, stdin EOF -> exit', p.returncode, 'in %.2fs' % (time.time() - t))
'@
    & $Python -c $code $Exe
    if ($LASTEXITCODE -ne 0) { throw "冒烟测试失败" }
}
