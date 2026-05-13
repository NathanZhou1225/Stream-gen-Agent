#!/usr/bin/env python3
"""今日市场快照 — 默认从本地数据库读取，加 --live-fetch 才实时抓取。

v0.2.2 架构：
- 默认：调用 finance-draft-manager/scripts/db_snapshot.py（DB 路径，不联网，快速）
- --live-fetch：调用 ingest.py legacy（旧 pipeline 路径，实时抓取，慢）

DB 为空/过期时直接返回提示，不自动触发网络请求。
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
DRAFT_MANAGER_ROOT = SKILL_ROOT.parent / "finance-draft-manager"
DB_SNAPSHOT_SCRIPT = DRAFT_MANAGER_ROOT / "scripts" / "db_snapshot.py"


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
            tail = raw[idx + end:].strip()
            if not tail or tail.startswith("\n"):
                last_obj = obj
    if last_obj is None:
        raise ValueError("stdout did not contain a JSON object")
    return last_obj


def _finance_python(finance_root: Path = FINANCE_ROOT) -> str:
    vpy = finance_root / ".venv" / "bin" / "python"
    return str(vpy if vpy.exists() else sys.executable)


# ── DB 路径（默认）────────────────────────────────────────────────────────────

def _run_db_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    """从本地 DB 读取快照，不联网。"""
    if not DB_SNAPSHOT_SCRIPT.exists():
        return {
            "ok": False,
            "error": {
                "code": "DB_SNAPSHOT_SCRIPT_MISSING",
                "message": f"db_snapshot.py 不存在: {DB_SNAPSHOT_SCRIPT}",
                "hint": "请确认 finance-draft-manager skill 已正确部署。",
            },
        }

    since_h = str(getattr(args, "since_hours", 24))
    cmd = [
        _finance_python(),
        str(DB_SNAPSHOT_SCRIPT),
        "--since-hours", since_h,
        "--sources", getattr(args, "sources", "market,news,social"),
        "--summary-only",
    ]
    msh = getattr(args, "major_since_hours", None)
    if msh is not None:
        cmd += ["--major-since-hours", str(msh)]
    if getattr(args, "no_router", False):
        cmd.append("--no-router")
    if getattr(args, "no_rewrite", False):
        cmd.append("--no-rewrite")
    if getattr(args, "keywords", ""):
        cmd += ["--keywords", args.keywords]

    env = _load_dotenv(dict(os.environ))
    db_timeout = int(getattr(args, "db_timeout", 180))
    proc = subprocess.run(
        cmd,
        cwd=str(WORKSPACE_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=db_timeout,
        check=False,
    )
    try:
        return _extract_json_object(proc.stdout)
    except ValueError:
        return {
            "ok": False,
            "error": {
                "code": "DB_SNAPSHOT_PARSE_ERROR",
                "message": "db_snapshot.py 输出无法解析",
                "hint": proc.stdout[-500:],
            },
        }


# ── legacy 路径（--live-fetch）────────────────────────────────────────────────

def _run_live_fetch(args: argparse.Namespace) -> dict[str, Any]:
    """实时抓取（旧 pipeline 路径），需要网络。"""
    python_bin = FINANCE_ROOT / ".venv" / "bin" / "python"
    cmd = [
        str(python_bin if python_bin.exists() else sys.executable),
        str(FINANCE_ROOT / "scripts" / "ingest.py"),
        "legacy",
        "--sources", getattr(args, "sources", "market,news,social"),
        "--max-items", str(getattr(args, "max_items", 30)),
    ]
    if getattr(args, "keywords", ""):
        cmd.extend(["--keywords", args.keywords])

    env = _load_dotenv(dict(os.environ))
    proc = subprocess.run(
        cmd,
        cwd=str(WORKSPACE_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=getattr(args, "timeout", 120),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"finance-source-ingest legacy failed ({proc.returncode}): {proc.stdout[-2000:]}")
    return _extract_json_object(proc.stdout)


def _build_summary_view(snap: dict[str, Any]) -> dict[str, Any]:
    meta = snap.get("meta") or {}
    return {
        "ok": bool(snap.get("ok", True)),
        "schema_version": snap.get("schema_version"),
        "meta": {
            "fetched_at": meta.get("fetched_at"),
            "timezone": meta.get("timezone"),
            "sources_requested": meta.get("sources_requested"),
            "sources_ok": meta.get("sources_ok"),
            "keywords": meta.get("keywords"),
            "data_source": meta.get("data_source", "live"),
            "db_last_ingested_at": meta.get("db_last_ingested_at"),
            "llm_router_status": meta.get("llm_router_status"),
            "llm_router_timing": meta.get("llm_router_timing"),
            "sector_llm_rewrite_status": meta.get("sector_llm_rewrite_status"),
            "major_since_hours": meta.get("major_since_hours"),
            "social_intelligence": meta.get("social_intelligence"),
        },
        "errors": snap.get("errors") or [],
        "markdown_summary": str(snap.get("markdown_summary") or ""),
        "invariants": snap.get("invariants"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="今日市场快照（默认从 DB 读取，不联网）",
    )
    parser.add_argument("--sources", default="market,news,social")
    parser.add_argument("--since-hours", type=int, default=24, help="DB 路径：新闻/板块主窗口（小时）")
    parser.add_argument("--major-since-hours", type=int, default=None, help="DB 路径：大事件窗口（默认 168）")
    parser.add_argument("--db-timeout", type=int, default=180, help="DB 路径：db_snapshot 子进程超时（秒）")
    parser.add_argument("--no-router", action="store_true", help="DB 路径：禁用 LLM Router")
    parser.add_argument("--no-rewrite", action="store_true", help="DB 路径：禁用板块润色")
    parser.add_argument("--keywords", default="")
    parser.add_argument("--max-items", type=int, default=30, help="仅 --live-fetch 时使用")
    parser.add_argument("--timeout", type=int, default=120, help="仅 --live-fetch 时使用")
    parser.add_argument(
        "--summary-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="仅输出轻量字段（默认 true）",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="输出完整 snapshot（等价 --no-summary-only）",
    )
    parser.add_argument(
        "--live-fetch",
        action="store_true",
        help="实时抓取（旧 pipeline 路径，需要网络，慢）。默认从本地 DB 读取。",
    )
    # 保留旧参数名以向后兼容
    parser.add_argument("--from-db", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.live_fetch:
        # 显式实时抓取
        try:
            snap = _run_live_fetch(args)
        except Exception as e:
            print(json.dumps({
                "ok": False,
                "error": {"code": "LIVE_FETCH_FAILED", "message": str(e)[:500]},
            }, ensure_ascii=False, indent=2))
            sys.exit(1)
    else:
        # 默认：DB 路径（含旧 --from-db 向后兼容）
        snap = _run_db_snapshot(args)

    summary_only = bool(args.summary_only) and not args.full
    payload = _build_summary_view(snap) if summary_only else snap
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
