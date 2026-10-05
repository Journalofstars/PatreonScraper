#!/usr/bin/env bash
# Patreon 内容下载器 —— 启动（Linux / macOS）
#
#   ./run.sh              启动程序
#   ./run.sh --software   显卡/驱动有问题导致内置浏览器黑屏时，改用软件渲染
#   ./run.sh --selftest   检查这份环境是否完整（Qt 插件、网络、内置浏览器）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "还没有安装运行环境，正在自动执行 setup.sh ..."
    echo
    bash ./setup.sh
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "[错误] 运行环境不可用，请先执行 ./setup.sh" >&2
    exit 1
fi

exec .venv/bin/python main.py "$@"
