#!/usr/bin/env python3
"""拉取（如需）并输出今日快照 markdown_summary，供 Agent 原样粘贴展示。

stdout：BEGIN/END 标记包裹的全文（禁止 Agent 改写或摘要）。
stderr：一行 meta + PASTE_VERBATIM 提示。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dump_markdown_summary import DEFAULT_MD, load_markdown
from platform_env import apply_python_utf8_mode

BEGIN_MARKER = "---SNAPSHOT_MARKDOWN_BEGIN---"
END_MARKER = "---SNAPSHOT_MARKDOWN_END---"


def main() -> None:
    apply_python_utf8_mode()
    p = argparse.ArgumentParser(description="展示今日信源快照 markdown（原样粘贴）")
    p.add_argument("--refresh", action="store_true", help="先 force-refresh 再输出")
    p.add_argument("--path", type=Path, default=DEFAULT_MD, help="侧车 .md 路径")
    args = p.parse_args()

    md_path = args.path.resolve()
    try:
        text, meta = load_markdown(refresh=bool(args.refresh), md_path=md_path)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    print(
        f"[present_today_snapshot] path={meta['path']} chars={meta['chars']} "
        "PASTE_VERBATIM — paste stdout between markers only, do not summarize",
        file=sys.stderr,
    )
    sys.stdout.write(f"{BEGIN_MARKER}\n")
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.write(f"{END_MARKER}\n")


if __name__ == "__main__":
    main()
