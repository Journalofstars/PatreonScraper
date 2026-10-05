@echo off
setlocal
cd /d "%~dp0"

rem 把项目打包成独立 exe（不需要装 Python 就能运行）。
rem 结果在 dist\PatreonDownloader\ ，双击里面的 PatreonDownloader.exe。

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

echo ============================================================
echo   正在打包为独立 exe，首次需要几分钟，请耐心等待
echo ============================================================
echo.

"%PY%" "%~dp0tools\build_exe.py" %*

echo.
echo 打包流程结束，退出码 %errorlevel%
echo 产物目录： %~dp0dist\PatreonDownloader\
echo.
pause
exit /b 0
