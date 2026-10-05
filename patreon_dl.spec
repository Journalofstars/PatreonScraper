# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir 模式）。

用法：
    .venv\\Scripts\\python.exe -m PyInstaller --clean --noconfirm patreon_dl.spec
或者直接跑封装好的脚本：
    .venv\\Scripts\\python.exe tools\\build_exe.py

为什么用 onedir 而不是 onefile：
    QtWebEngine 有 400 MB 左右的运行时（Chromium、资源包、语言文件）。
    onefile 每次启动都要把它们解压到临时目录，启动要十几秒、还容易被杀软误报；
    onedir 启动快，也方便排查缺哪个 dll。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs

APP_NAME = "PatreonDownloader"
CONSOLE = os.environ.get("PATREON_DL_CONSOLE") == "1"

# QtWebEngine 的资源/进程文件由 PyInstaller 的 PySide6 钩子自动收集，
# 这里只兜底把动态库再收一遍，避免某些版本漏掉。
binaries = []
try:
    binaries += collect_dynamic_libs("PySide6")
except Exception:  # noqa: BLE001
    pass

hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtPrintSupport",
    "PySide6.QtSvg",
]

# 明显用不到的大块头，剔掉能显著减小体积
excludes = [
    "tkinter",
    "unittest",
    "pydoc_data",
    "numpy",
    "pandas",
    "matplotlib",
    "PIL",
    "lxml",
    "openpyxl",
    "docx",
    "pptx",
    "PySide6.QtQuick3D",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtBluetooth",
    "PySide6.QtDesigner",
    "PySide6.QtTest",
]

a = Analysis(
    ["main.py"],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ------------------------------------------------------------------ 体积精简
# Chromium 的 *.debug.pak / *.debug.bin 只在调试版里用到，正式运行不会加载；
# QtWebEngine 的 53 个语言包只需要保留 en-US 与 zh-CN；
# Qt 自带翻译只留简体中文（英文是内置的，不需要 .qm 文件）。
# 这三项合计能省下 120 MB 以上。

def _is_trimmable(entry) -> bool:
    raw = str(entry[0])
    name = raw.replace("\\", "/").lower()
    if name.endswith(".debug.pak") or name.endswith(".debug.bin"):
        return True
    if "qtwebengine_locales/" in name:
        return not name.endswith(("en-us.pak", "zh-cn.pak"))
    if "/translations/" in name and name.endswith(".qm"):
        return "_zh_cn" not in name
    return False


_before = len(a.datas)
a.datas = [item for item in a.datas if not _is_trimmable(item)]
_removed = _before - len(a.datas)
print(f"[spec] 精简数据文件：移除 {_removed} 个（保留 {len(a.datas)} 个）")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(SPECPATH, "assets", "app.ico")
    if os.path.exists(os.path.join(SPECPATH, "assets", "app.ico"))
    else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
