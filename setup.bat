@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Patreon 内容下载器 - 安装依赖
echo ============================================================
echo.

set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE where python >nul 2>nul && set "PYEXE=python"

if not defined PYEXE (
    echo [错误] 系统里没有找到 Python。
    echo        请先安装 Python 3.9 或更高版本： https://www.python.org/downloads/
    echo        安装时记得勾选 Add Python to PATH。
    echo.
    pause
    exit /b 1
)

echo 使用的 Python：
%PYEXE% -c "import sys; print('    ', sys.executable); print('    版本', sys.version.split()[0])"
echo.

if exist ".venv\Scripts\python.exe" (
    echo 已存在虚拟环境 .venv，跳过创建。
) else (
    echo 正在创建虚拟环境 .venv ...
    %PYEXE% -m venv .venv
    if errorlevel 1 goto failed
)

echo.
echo 正在安装依赖，首次需要下载约 250 MB，请耐心等待 ...
echo.
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

echo.
echo 正在自检 ...
".venv\Scripts\python.exe" -c "import PySide6, requests; from PySide6.QtWebEngineWidgets import QWebEngineView; print('    PySide6', PySide6.__version__, '/ requests', requests.__version__, '/ 内置浏览器可用')"
if errorlevel 1 (
    echo.
    echo [警告] 内置浏览器组件加载失败，仍可使用手动粘贴 Cookie 方式登录。
)

echo.
echo ============================================================
echo   安装完成，以后双击 run.bat 即可启动程序。
echo ============================================================
echo.
pause
exit /b 0

:failed
echo.
echo [错误] 安装失败。
echo        请检查网络连接，或配置 pip 国内镜像后重试；
echo        并确认 Python 版本不低于 3.9。
echo.
pause
exit /b 1
