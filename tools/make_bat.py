"""重新生成 run.bat / run-debug.bat / setup.bat / build.bat（GBK + CRLF）。

为什么需要这个脚本
------------------
Windows 的 ``cmd.exe`` 用系统 ANSI 代码页（简体中文是 936/GBK）逐行读取批处理文件：

* 如果 ``.bat`` 存成 **UTF-8**，中文的多字节序列会被当成 GBK 双字节字符，
  一个中文字符会和后面的 ASCII 字节配对，把命令本身吃掉，
  于是出现 ``'et' 不是内部或外部命令``（``set`` 被读成 ``et``）这类怪错；
* 批处理还要求 **CRLF** 换行，纯 LF 会造成错行；
* 在批处理里调用 ``chcp`` 会切换代码页，而 cmd 仍按字节偏移继续读文件，
  同样会把后续命令读错。

所以这里的做法是：**GBK 编码 + CRLF 换行 + 不调用 chcp**。
GBK 是 ASCII 的超集（单字节部分完全一致），即使在某些非中文环境下中文显示成乱码，
命令本身依然能正确执行。

用法::

    .venv\\Scripts\\python.exe tools\\make_bat.py

改完批处理内容后重新运行一次即可；脚本会校验换行与编码。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RUN_BAT = r"""@echo off
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
"""

RUN_DEBUG_BAT = r"""@echo off
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
"""

SETUP_BAT = r"""@echo off
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
"""

BUILD_BAT = r"""@echo off
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
"""

FILES = (
    ("run.bat", RUN_BAT),
    ("run-debug.bat", RUN_DEBUG_BAT),
    ("setup.bat", SETUP_BAT),
    ("build.bat", BUILD_BAT),
)


def write_bat(name: str, content: str) -> tuple[str, int]:
    path = os.path.join(ROOT, name)
    text = content.replace("\r\n", "\n").replace("\n", "\r\n")
    data = text.encode("gbk")
    with open(path, "wb") as handle:
        handle.write(data)
    return path, len(data)


def main() -> int:
    ok = True
    for name, content in FILES:
        path, size = write_bat(name, content)
        raw = open(path, "rb").read()
        crlf = raw.count(b"\r\n")
        bare_lf = raw.count(b"\n") - crlf
        try:
            raw.decode("gbk")
            enc_ok = True
        except UnicodeDecodeError:
            enc_ok = False
        status = "OK" if (bare_lf == 0 and enc_ok) else "错误"
        print(f"  {name:<15} {size:>5} 字节  CRLF={crlf:<4} 裸LF={bare_lf}  编码GBK={enc_ok}  [{status}]")
        if bare_lf or not enc_ok:
            ok = False
    print("完成。" if ok else "存在问题，请检查上面的输出。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
