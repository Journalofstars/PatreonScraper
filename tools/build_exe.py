"""打包成独立 exe（PyInstaller，onedir 模式）。

用法::

    .venv\\Scripts\\python.exe tools\\build_exe.py            # 无控制台窗口
    .venv\\Scripts\\python.exe tools\\build_exe.py --console  # 带控制台，便于排查
    .venv\\Scripts\\python.exe tools\\build_exe.py --fresh    # 先重装 PyInstaller

产物在 ``dist\\PatreonDownloader\\``，双击里面的 ``PatreonDownloader.exe`` 即可。
整个目录可以拷到任何 Windows 机器上运行，**不需要装 Python**。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "PatreonDownloader"


def log(message: str) -> None:
    print(message, flush=True)


def python_executable() -> str:
    """优先用项目自带的虚拟环境。"""
    if VENV_PY.exists():
        return str(VENV_PY)
    return sys.executable


def ensure_pyinstaller(python: str, fresh: bool = False) -> None:
    check = subprocess.run(
        [python, "-c", "import PyInstaller; print(PyInstaller.__version__)"],
        capture_output=True, text=True,
    )
    if check.returncode == 0 and not fresh:
        log(f"    PyInstaller {check.stdout.strip()} 已就绪")
        return
    log("    正在安装 PyInstaller …")
    subprocess.run(
        [python, "-m", "pip", "install", "--upgrade", "pyinstaller"],
        check=True,
    )


def clean() -> None:
    for path in (DIST, BUILD):
        if path.exists():
            log(f"    清理 {path.name}\\")
            shutil.rmtree(path, ignore_errors=True)


def run_pyinstaller(python: str, console: bool) -> None:
    env = dict(os.environ)
    env["PATREON_DL_CONSOLE"] = "1" if console else "0"
    env["PYTHONUTF8"] = "1"
    command = [
        python, "-m", "PyInstaller",
        "--clean", "--noconfirm",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        str(ROOT / "patreon_dl.spec"),
    ]
    log("    执行: " + " ".join(command[1:]))
    started = time.time()
    completed = subprocess.run(command, cwd=str(ROOT), env=env)
    if completed.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {completed.returncode}")
    log(f"    打包完成，用时 {time.time() - started:.0f} 秒")


def post_process() -> None:
    """把说明文档放进产物目录，并写一个中文启动脚本。"""
    target = DIST / APP_NAME
    if not target.is_dir():
        raise SystemExit(f"没有找到产物目录：{target}")

    for name in ("README.md", "LICENSE", "requirements.txt"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, target / name)
    docs = ROOT / "docs"
    if docs.is_dir():
        shutil.copytree(docs, target / "docs", dirs_exist_ok=True)

    launcher = target / "启动.bat"
    launcher.write_bytes(
        (
            "@echo off\r\n"
            "cd /d \"%~dp0\"\r\n"
            f"start \"\" \"{APP_NAME}.exe\"\r\n"
        ).encode("gbk")
    )
    (target / "data").mkdir(exist_ok=True)

    exe = target / f"{APP_NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"没有找到可执行文件：{exe}")


def report() -> None:
    target = DIST / APP_NAME
    total = 0
    files = 0
    biggest = []
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        files += 1
        biggest.append((size, path.relative_to(target)))
    biggest.sort(reverse=True)
    log("")
    log("=" * 62)
    log(f"  产物目录 : {target}")
    log(f"  文件数量 : {files}")
    log(f"  总体积   : {total / 1048576:.1f} MB")
    log("  最大的几个文件：")
    for size, rel in biggest[:6]:
        log(f"    {size / 1048576:>8.1f} MB  {rel}")
    log("=" * 62)
    log(f"  双击运行 : {target / (APP_NAME + '.exe')}")
    log("  数据目录 : 与 exe 同级，首次运行自动创建")
    log("=" * 62)


def main() -> int:
    parser = argparse.ArgumentParser(description="把项目打包成独立 exe")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口，便于看报错")
    parser.add_argument("--fresh", action="store_true", help="强制重装 PyInstaller")
    parser.add_argument("--keep-build", action="store_true", help="保留中间产物 build\\")
    args = parser.parse_args()

    if os.name != "nt":
        log("提示：这个脚本主要针对 Windows；其它平台产物目录结构相同但需自行验证。")

    python = python_executable()
    log("=" * 62)
    log(f"  Python : {python}")
    log(f"  项目根 : {ROOT}")
    log("=" * 62)

    ensure_pyinstaller(python, fresh=args.fresh)
    clean()
    run_pyinstaller(python, console=args.console)
    post_process()
    if not args.keep_build and BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)
    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
