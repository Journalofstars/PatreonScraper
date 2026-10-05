@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"

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

if exist "%PYW%" (
    start "" "%PYW%" "%~dp0main.py" %*
) else (
    "%PY%" "%~dp0main.py" %*
)
exit /b 0
