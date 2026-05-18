#!/usr/bin/env python3
"""finance-source-ingest — 联网 legacy 与 RSSHub 修复（本地 SQLite Newsbox 已移除）。

用法：
  ingest.py legacy [--sources market,news,social] [--keywords ...] [--max-items 30] [--out-dir PATH]
  ingest.py repair-rsshub [--decision confirm|ignore] [--token TOKEN]

定时入库仅在运维机 ``finance-ingest-cloud/worker/run_ingest.sh``（MySQL）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from _venv_bootstrap import ensure_venv_and_reexec

    ensure_venv_and_reexec(Path(__file__).resolve())
except ImportError:
    pass

WORKSPACE_ROOT = ROOT.parent.parent


def _load_dotenv() -> None:
    for path in (
        WORKSPACE_ROOT / ".env",
        WORKSPACE_ROOT.parent / ".env",
        Path("/root/.openclaw/.env"),
    ):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def cmd_legacy(args: argparse.Namespace) -> None:
    """实时 pipeline.build_snapshot（供 query_market_facts --live-fetch / preflight legacy）。"""
    try:
        from pipeline import build_snapshot
    except ImportError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "PIPELINE_UNAVAILABLE",
                        "message": "pipeline.py 不可用",
                        "hint": "日常拉数请使用 query_market_facts.py（云端 API）；legacy 仅用于显式 --live-fetch",
                    },
                },
                ensure_ascii=False,
            )
        )
        sys.exit(2)

    sources = [s.strip() for s in (args.sources or "market,news,social").split(",") if s.strip()]
    keywords = [k for k in (args.keywords or "").split() if k.strip()]
    snap = build_snapshot(sources, keywords, args.max_items)

    from _common import emit_json, write_json_atomic, write_text_atomic

    emit_json(snap)
    out_dir = (args.out_dir or "").strip()
    if out_dir:
        outp = Path(out_dir)
        write_json_atomic(outp / "snapshot.json", snap)
        write_text_atomic(outp / "snapshot.md", snap.get("markdown_summary", ""))


def cmd_repair_rsshub(args: argparse.Namespace) -> None:
    from scripts.ingest_legacy import cmd_repair_rsshub as _legacy_repair

    _legacy_repair(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="finance-source-ingest（legacy + repair-rsshub；Newsbox 入库见 finance-ingest-cloud）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    leg_p = sub.add_parser("legacy", help="实时 pipeline build_snapshot（慢，需网络）")
    leg_p.add_argument("--sources", default="market,news,social")
    leg_p.add_argument("--keywords", default="")
    leg_p.add_argument("--max-items", type=int, default=30)
    leg_p.add_argument("--out-dir", default="")
    leg_p.set_defaults(func=cmd_legacy)

    repair_p = sub.add_parser("repair-rsshub", help="RSSHub 修复回调")
    repair_p.add_argument("--decision", default="ignore", choices=["confirm", "execute", "yes", "ignore", "no"])
    repair_p.add_argument("--sources", default="news")
    repair_p.add_argument("--keywords", default="")
    repair_p.add_argument("--max-items", type=int, default=30)
    repair_p.add_argument("--update-timeout", type=int, default=600)
    repair_p.add_argument("--token", default="")
    repair_p.set_defaults(func=cmd_repair_rsshub)

    args = parser.parse_args()
    fn = getattr(args, "func", None)
    if fn is None:
        parser.print_help()
        sys.exit(2)
    fn(args)


if __name__ == "__main__":
    main()
