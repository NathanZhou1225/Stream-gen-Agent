#!/usr/bin/env python3
"""Public entry: ingest run -> JSON on stdout; optional --out-dir."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 首次用系统 python 调用时：自动建 .venv 并 install -r，再切到 venv 解释器（可 FINANCE_INGEST_NO_AUTO_VENV=1 关闭）
from _venv_bootstrap import ensure_venv_and_reexec

ensure_venv_and_reexec(Path(__file__).resolve())

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _common import emit_json, write_json_atomic, write_text_atomic
from pipeline import build_snapshot


def cmd_run(args: argparse.Namespace) -> None:
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    keywords = [k for k in args.keywords.split()] if args.keywords else []
    snap = build_snapshot(sources, keywords, args.max_items)
    emit_json(snap)
    out_dir = (args.out_dir or "").strip()
    if out_dir:
        outp = Path(out_dir)
        write_json_atomic(outp / "snapshot.json", snap)
        write_text_atomic(outp / "snapshot.md", snap.get("markdown_summary", ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="finance-source-ingest")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Fetch sources and print JSON")
    run_p.add_argument("--sources", default="market,news,social")
    run_p.add_argument("--keywords", default="")
    run_p.add_argument("--max-items", type=int, default=30)
    run_p.add_argument("--out-dir", default="")
    run_p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    fn = getattr(args, "func", None)
    if fn is None:
        parser.print_help()
        sys.exit(2)
    fn(args)


if __name__ == "__main__":
    main()
