@echo off
chcp 65001 >nul
title RPGBar 服务器
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python。请先安装 Python 3.10+，并运行：
    echo        pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo 正在启动 RPGBar 服务器...
echo 启动后，把下面打印的「局域网访问」地址发给朋友，即可一起玩。
echo.
python -m server.main
pause
