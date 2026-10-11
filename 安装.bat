@echo off
cd /d "%~dp0"
echo === 简易版（tkinter）安装 ===
echo 正式版请到 GitHub Releases 下载 Kikitori.exe，不需要运行本脚本。
echo.
echo === 创建 Python 虚拟环境 ===
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 (
        echo 创建失败：请先安装 Python 3.11 以上版本
        pause
        exit /b 1
    )
)
echo === 安装依赖，约 1.5GB，主要是显卡加速库 ===
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo 安装失败，请检查网络
    pause
    exit /b 1
)
if not exist "config.toml" copy /y "config.example.toml" "config.toml" >nul
echo.
echo 安装完成！接下来：
echo   1. 用记事本打开 config.toml，填好 DeepSeek 的 api_key
echo   2. 双击 启动.bat
pause
