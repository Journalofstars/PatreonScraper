#!/usr/bin/env bash
# Patreon 内容下载器 —— 安装依赖（Linux / macOS）
#
#   ./setup.sh
#
# 需要 Python 3.9 或更高版本。可以用环境变量指定解释器：
#   PYTHON=python3.12 ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================================"
echo "  Patreon 内容下载器 - 安装依赖"
echo "============================================================"
echo

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "[错误] 找不到 $PYTHON。" >&2
    echo "       请先安装 Python 3.9 或更高版本：https://www.python.org/downloads/" >&2
    exit 1
fi

echo "使用的 Python："
"$PYTHON" -c 'import sys; print("    ", sys.executable); print("    版本", sys.version.split()[0])'
echo

if [ -x ".venv/bin/python" ]; then
    echo "已存在虚拟环境 .venv，跳过创建。"
else
    echo "正在创建虚拟环境 .venv ..."
    "$PYTHON" -m venv .venv
fi

echo
echo "正在安装依赖（首次需要下载约 250 MB，请耐心等待）..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo
echo "正在自检 ..."
if .venv/bin/python -c "import PySide6, requests; from PySide6.QtWebEngineWidgets import QWebEngineView; print('    PySide6', PySide6.__version__, '/ requests', requests.__version__, '/ 内置浏览器可用')"; then
    :
else
    echo "    [警告] 内置浏览器组件加载失败，仍可使用「手动粘贴 Cookie」方式登录。"
    echo "           常见原因是缺少系统库，例如 Ubuntu/Debian 需要："
    echo "           sudo apt install libnss3 libxkbcommon-x11-0 libegl1 libgl1 libasound2"
fi

echo
echo "============================================================"
echo "  安装完成，运行 ./run.sh 启动程序。"
echo "============================================================"
