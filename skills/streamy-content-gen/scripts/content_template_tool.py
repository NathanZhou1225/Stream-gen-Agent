#!/usr/bin/env python3
"""从任意 cwd 调用稿件类型模板 CLI（将 skill 根目录加入 sys.path）。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from generators.script_generator import main

if __name__ == "__main__":
    raise SystemExit(main())
