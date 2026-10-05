@echo off
setlocal
cd /d "%~dp0"

rem 带控制台窗口启动，方便查看报错信息。

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo.
    echo 还没有安装运行环境，正在自动执行 setup.bat ...
    echo.
    call "%~dp0setup.bat"
)

if not exist "%PY%" (
    echo.
    echo [错误] 运行环境不可用，请手动双击 setup.bat 安装。
    echo.
    pause
    exit /b 1
)

"%PY%" "%~dp0main.py" %*

echo.
echo 程序已退出，退出码 %errorlevel%
echo.
pause
exit /b 0
