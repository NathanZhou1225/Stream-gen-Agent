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

from _common import get_workspace_root
from platform_env import apply_python_utf8_mode

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = get_workspace_root()
QMF = SCRIPT_DIR / "query_market_facts.py"
DEFAULT_MD = WORKSPACE_ROOT / "cache" / "snapshot" / "markdown_summary.md"

MARKDOWN_ANCHORS = ("今日信源全量快照", "大盘与情绪")


def validate_markdown(text: str) -> list[str]:
    """返回缺失的锚点标题（空列表 = 通过）。"""
    missing: list[str] = []
    for anchor in MARKDOWN_ANCHORS:
        if anchor not in text:
            missing.append(anchor)
    return missing


def _run_qmf(*, force_refresh: bool) -> None:
    cmd = [
        sys.executable,
        str(QMF),
        "--sources",
        "market,news,social",
        "--summary-only",
    ]
    if force_refresh:
        cmd.append("--force-refresh")
    subprocess.run(
        cmd,
        cwd=str(WORKSPACE_ROOT),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def load_markdown(*, refresh: bool, md_path: Path) -> tuple[str, dict[str, str]]:
    """确保侧车存在并返回 (text, meta)。"""
    if refresh:
        _run_qmf(force_refresh=True)
    elif not md_path.is_file():
        _run_qmf(force_refresh=False)

    if not md_path.is_file():
        raise FileNotFoundError(
            f"markdown_summary 不可用：{md_path}（请先成功执行 query_market_facts）"
        )

    text = md_path.read_text(encoding="utf-8")
    missing = validate_markdown(text)
    if missing:
        raise ValueError(
            f"markdown_summary 缺少锚点：{', '.join(missing)}；"
            "请执行 query_market_facts --force-refresh 后重试"
        )
    meta = {
        "path": str(md_path.resolve()),
        "chars": str(len(text)),
        "anchors_ok": "1",
    }
    return text, meta


def main() -> None:
    apply_python_utf8_mode()
    p = argparse.ArgumentParser(description="打印 markdown_summary 纯文本（UTF-8）")
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
        f"[dump_markdown_summary] path={meta['path']} chars={meta['chars']} PASTE_VERBATIM",
        file=sys.stderr,
    )
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
