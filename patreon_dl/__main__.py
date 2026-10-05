"""``python -m patreon_dl`` 入口。"""

from __future__ import annotations

import sys

from .ui.app import run

if __name__ == "__main__":
    sys.exit(run())
