#!/usr/bin/env python3
"""Run finance-source-ingest and print a JSON snapshot on stdout.

默认走 summary-only 轻输出，减少会话上下文负担与 token 消耗。
可用 --full 回退输出完整 snapshot（调试/排障用）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SKILL_ROOT.parent.parent
FINANCE_ROOT = SKILL_ROOT.parent / "finance-source-ingest"


def _load_dotenv(env: dict[str, str]) -> dict[str, str]:
    for candidate in (
        WORKSPACE_ROOT / ".env",
        WORKSPACE_ROOT.parent / ".env",
        Path("/root/.openclaw/.env"),
    ):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key and key not in env:
                env[key] = value
    return env


def _extract_json_object(raw: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    last_obj: dict[str, Any] | None = None
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, end = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            tail = raw[idx + end :].strip()
            if not tail or tail.startswith("\n"):
                last_obj = obj
    if last_obj is None:
        raise ValueError("finance-source-ingest stdout did not contain a JSON object")
    return last_obj


def _run_finance_ingest(args: argparse.Namespace) -> dict[str, Any]:
    python_bin = FINANCE_ROOT / ".venv" / "bin" / "python"
    cmd = [
        str(python_bin if python_bin.exists() else sys.executable),
        str(FINANCE_ROOT / "scripts" / "ingest.py"),
        "run",
        "--sources",
        args.sources,
        "--max-items",
        str(args.max_items),
    ]
    if args.keywords:
        cmd.extend(["--keywords", args.keywords])

    env = _load_dotenv(dict(os.environ))
    proc = subprocess.run(
        cmd,
        cwd=str(WORKSPACE_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=args.timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"finance-source-ingest failed ({proc.returncode}): {proc.stdout[-2000:]}")
    return _extract_json_object(proc.stdout)


def _build_summary_view(snap: dict[str, Any]) -> dict[str, Any]:
    meta = snap.get("meta") or {}
    errors = snap.get("errors") or []
    summary = str(snap.get("markdown_summary") or "")
    out: dict[str, Any] = {
        "ok": bool(snap.get("ok", True)),
        "schema_version": snap.get("schema_version"),
        "meta": {
            "fetched_at": meta.get("fetched_at"),
            "timezone": meta.get("timezone"),
            "sources_requested": meta.get("sources_requested"),
            "sources_ok": meta.get("sources_ok"),
            "keywords": meta.get("keywords"),
            # 便于诊断慢点：直接带 router/rewrite timing（如存在）
            "llm_router_status": meta.get("llm_router_status"),
            "llm_router_timing": meta.get("llm_router_timing"),
            "sector_llm_rewrite_status": meta.get("sector_llm_rewrite_status"),
            "sector_llm_rewrite_timing": meta.get("sector_llm_rewrite_timing"),
        },
        "errors": errors,
        "markdown_summary": summary,
        "invariants": snap.get("invariants"),
    }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Run market facts (finance-source-ingest JSON on stdout)")
    parser.add_argument("--sources", default="market,news,social")
    parser.add_argument("--keywords", default="")
    parser.add_argument("--max-items", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--summary-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="仅输出轻量字段（默认 true，减少上下文与 token）。",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="输出完整 snapshot（等价 --no-summary-only）。",
    )
    args = parser.parse_args()

    snap = _run_finance_ingest(args)
    summary_only = bool(args.summary_only)
    if args.full:
        summary_only = False
    payload = _build_summary_view(snap) if summary_only else snap
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
