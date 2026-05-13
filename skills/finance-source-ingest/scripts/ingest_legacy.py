#!/usr/bin/env python3
"""Public entry: ingest run -> JSON on stdout; optional --out-dir."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# 首次用系统 python 调用时：自动建 .venv 并 install -r，再切到 venv 解释器（可 FINANCE_INGEST_NO_AUTO_VENV=1 关闭）
from _venv_bootstrap import ensure_venv_and_reexec

ensure_venv_and_reexec(Path(__file__).resolve())

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _merge_workspace_dotenv() -> None:
    """将 workspace / openclaw 根目录 .env 合并进 os.environ（不覆盖已存在变量）。"""
    here = Path(__file__).resolve()
    for path in (
        here.parents[3] / ".env",
        here.parents[4] / ".env",
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


_merge_workspace_dotenv()

from _common import emit_json, write_json_atomic, write_text_atomic
from pipeline import build_snapshot

RSSHUB_UPDATE_SCRIPT = "/root/.openclaw/workspace-stream-gen/rsshub/update_rsshub.sh"


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


def _check_callback_token(token: str) -> None:
    expected = os.environ.get("FINANCE_RSSHUB_CALLBACK_TOKEN", "").strip()
    if not expected:
        return
    if not token or token != expected:
        raise RuntimeError("unauthorized callback token")


def _run_update_script(timeout_sec: int) -> tuple[bool, int]:
    if not Path(RSSHUB_UPDATE_SCRIPT).exists():
        return False, -1
    try:
        proc = subprocess.run(
            [RSSHUB_UPDATE_SCRIPT],
            text=True,
            check=False,
            timeout=max(10, int(timeout_sec)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc.returncode == 0, proc.returncode
    except Exception:
        return False, -1


def cmd_repair_rsshub(args: argparse.Namespace) -> None:
    """回调执行器：供飞书/微信按钮回调触发 RSSHub 修复。"""
    _check_callback_token((args.token or "").strip())

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    keywords = [k for k in args.keywords.split()] if args.keywords else []
    decision = (args.decision or "").strip().lower()

    callback_meta: dict[str, object] = {
        "callback_interface": "repair-rsshub",
        "decision": decision,
        "update_script": RSSHUB_UPDATE_SCRIPT,
    }

    if decision in {"confirm", "execute", "yes"}:
        ok, code = _run_update_script(args.update_timeout)
        callback_meta["update_executed"] = True
        callback_meta["update_ok"] = ok
        callback_meta["update_exit_code"] = code
    else:
        callback_meta["update_executed"] = False
        callback_meta["update_ok"] = False
        callback_meta["update_exit_code"] = None

    snap = build_snapshot(sources, keywords, args.max_items)
    meta = snap.get("meta") or {}
    if isinstance(meta, dict):
        meta["rsshub_callback"] = callback_meta
        snap["meta"] = meta
    emit_json(snap)


def main() -> None:
    parser = argparse.ArgumentParser(description="finance-source-ingest")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Fetch sources and print JSON")
    run_p.add_argument("--sources", default="market,news,social")
    run_p.add_argument("--keywords", default="")
    run_p.add_argument("--max-items", type=int, default=30)
    run_p.add_argument("--out-dir", default="")
    run_p.set_defaults(func=cmd_run)

    repair_p = sub.add_parser("repair-rsshub", help="Execute RSSHub update callback and retry fetch")
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
