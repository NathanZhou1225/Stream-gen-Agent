#!/usr/bin/env python3
"""输出今日快照 markdown_summary（UTF-8 纯文本，供 WorkBuddy 直接 read，避免 JSON/控制台乱码）。

用法（workspace-stream-gen 根）：
  python3 skills/streamy-content-gen/scripts/dump_markdown_summary.py
  python3 skills/streamy-content-gen/scripts/dump_markdown_summary.py --refresh
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent.parent
QMF = SCRIPT_DIR / "query_market_facts.py"
DEFAULT_MD = WORKSPACE_ROOT / "cache" / "snapshot" / "markdown_summary.md"


from platform_env import apply_python_utf8_mode


def main() -> None:
    apply_python_utf8_mode()
    p = argparse.ArgumentParser(description="打印 markdown_summary 纯文本（UTF-8）")
    p.add_argument("--refresh", action="store_true", help="先 force-refresh 再输出")
    p.add_argument("--path", type=Path, default=DEFAULT_MD, help="侧车 .md 路径")
    args = p.parse_args()

    if args.refresh:
        cmd = [
            sys.executable,
            str(QMF),
            "--sources",
            "market,news,social",
            "--summary-only",
            "--force-refresh",
        ]
        subprocess.run(
            cmd,
            cwd=str(WORKSPACE_ROOT),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )

    md_path = args.path.resolve()
    if not md_path.is_file():
        cmd = [
            sys.executable,
            str(QMF),
            "--sources",
            "market,news,social",
            "--summary-only",
        ]
        subprocess.run(cmd, cwd=str(WORKSPACE_ROOT), check=False, timeout=300)
    if not md_path.is_file():
        print("markdown_summary 不可用：请先成功执行 query_market_facts", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(md_path.read_text(encoding="utf-8"))
    if not sys.stdout.isatty():
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
