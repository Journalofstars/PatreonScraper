#!/usr/bin/env python
"""Patreon 内容下载器 —— 桌面图形界面启动入口。

直接运行：
    python main.py
或使用附带的一键脚本：
    run.bat
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patreon_dl.ui.app import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
