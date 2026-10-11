@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo 还没有安装运行环境，请先双击运行 安装.bat
    pause
    exit /b 1
)
rem 用 pythonw 在后台运行：这个黑窗口会马上关掉，字幕条上右键 → 退出 才会真正关闭程序
start "" ".venv\Scripts\pythonw.exe" main.py %*
