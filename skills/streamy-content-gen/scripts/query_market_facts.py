#!/usr/bin/env python3
"""今日市场快照 — 默认从本地数据库读取，加 --live-fetch 才实时抓取。

v0.2.2 架构：
- 默认：调用 finance-draft-manager/scripts/db_snapshot.py（DB 路径，不联网，快速）
- --cloud / FINANCE_CLOUD_MODE=1：HTTP 拉云端 pre-Router JSON → 本地 db_snapshot Router/Rewriter
- --live-fetch：调用 ingest.py legacy（旧 pipeline 路径，实时抓取，慢）

DB 为空/过期时直接返回提示，不自动触发网络请求。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SKILL_ROOT.parent.parent
FINANCE_ROOT = SKILL_ROOT.parent / "finance-source-ingest"
DRAFT_MANAGER_ROOT = SKILL_ROOT.parent / "finance-draft-manager"
DB_SNAPSHOT_SCRIPT = DRAFT_MANAGER_ROOT / "scripts" / "db_snapshot.py"


def _load_dotenv(env: dict[str, str] | None = None) -> dict[str, str]:
    """合并 .env；``workspace-stream-gen/.env`` 中的键覆盖上级/Shell（与 Router 配置一致）。"""
    merged = dict(os.environ if env is None else env)
    workspace_env = (WORKSPACE_ROOT / ".env").resolve()
    for candidate in (
        WORKSPACE_ROOT.parent / ".env",
        Path("/root/.openclaw/.env"),
        WORKSPACE_ROOT / ".env",
    ):
        if not candidate.exists():
            continue
        force = candidate.resolve() == workspace_env
        for line in candidate.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and (force or key not in merged):
                merged[key] = value
    return merged


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


def _cloud_mode_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "cloud", False):
        return True
    v = os.environ.get("FINANCE_CLOUD_MODE", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _cloud_config_error(env: dict[str, str]) -> dict[str, Any]:
    base = (env.get("FINANCE_CLOUD_API_BASE_URL") or "").strip()
    key = (env.get("FINANCE_CLOUD_API_KEY") or "").strip()
    missing = []
    if not base:
        missing.append("FINANCE_CLOUD_API_BASE_URL")
    if not key:
        missing.append("FINANCE_CLOUD_API_KEY")
    return {
        "ok": False,
        "error": {
            "code": "CLOUD_CONFIG_MISSING",
            "message": f"云模式缺少环境变量：{', '.join(missing)}",
            "hint": "在 workspace-stream-gen/.env 配置云端 API 基址与 Bearer Key；云端服务见 finance-ingest-cloud/README.md",
        },
    }


def _fetch_cloud_pre_router(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    base = (env.get("FINANCE_CLOUD_API_BASE_URL") or "").strip().rstrip("/")
    key = (env.get("FINANCE_CLOUD_API_KEY") or "").strip()
    if not base or not key:
        return _cloud_config_error(env)

    params: dict[str, str | int] = {
        "since_hours": getattr(args, "since_hours", 24),
        "sources": getattr(args, "sources", "market,news,social"),
    }
    msh = getattr(args, "major_since_hours", None)
    if msh is not None:
        params["major_since_hours"] = msh
    kw = getattr(args, "keywords", "") or ""
    if kw.strip():
        params["keywords"] = kw.strip()

    url = f"{base}/api/v1/market-facts?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        method="GET",
    )
    timeout = int(env.get("FINANCE_CLOUD_API_TIMEOUT", "60"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return {
            "ok": False,
            "error": {
                "code": "CLOUD_HTTP_ERROR",
                "message": f"云端 API HTTP {exc.code}",
                "hint": detail,
            },
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": {
                "code": "CLOUD_UNREACHABLE",
                "message": str(exc.reason)[:400] if getattr(exc, "reason", None) else str(exc)[:400],
                "hint": f"请确认 {base} 可达且 finance-ingest-cloud API 已启动（./run_api.sh）",
            },
        }

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": {
                "code": "CLOUD_JSON_INVALID",
                "message": str(exc)[:300],
                "hint": body[:500],
            },
        }


# ── 云路径（--cloud / FINANCE_CLOUD_MODE=1）──────────────────────────────────

def _run_cloud_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    """HTTP 拉 pre-Router → 本地 db_snapshot 套 Router/Rewriter。"""
    if not DB_SNAPSHOT_SCRIPT.exists():
        return {
            "ok": False,
            "error": {
                "code": "DB_SNAPSHOT_SCRIPT_MISSING",
                "message": f"db_snapshot.py 不存在: {DB_SNAPSHOT_SCRIPT}",
            },
        }

    env = _load_dotenv(dict(os.environ))
    cloud = _fetch_cloud_pre_router(args, env)
    if cloud.get("error"):
        return cloud
    if not isinstance(cloud.get("sections"), dict):
        return {
            "ok": False,
            "error": {
                "code": "CLOUD_PAYLOAD_INVALID",
                "message": "云端响应缺少 sections",
                "hint": str(cloud)[:400],
            },
        }

    since_h = str(getattr(args, "since_hours", 24))
    cmd = [
        _finance_python(),
        str(DB_SNAPSHOT_SCRIPT),
        "--pre-router-stdin",
        "--since-hours", since_h,
        "--summary-only",
    ]
    msh = getattr(args, "major_since_hours", None)
    if msh is not None:
        cmd += ["--major-since-hours", str(msh)]
    if getattr(args, "no_router", False):
        cmd.append("--no-router")
    if getattr(args, "no_rewrite", False):
        cmd.append("--no-rewrite")

    db_timeout = int(getattr(args, "db_timeout", 180))
    proc = subprocess.run(
        cmd,
        cwd=str(WORKSPACE_ROOT),
        env=env,
        input=json.dumps(cloud, ensure_ascii=False),
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
                "code": "CLOUD_SNAPSHOT_PARSE_ERROR",
                "message": "db_snapshot --pre-router-stdin 输出无法解析",
                "hint": proc.stdout[-500:],
            },
        }


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
            "cloud_schema_version": meta.get("cloud_schema_version"),
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
    parser.add_argument(
        "--cloud",
        action="store_true",
        help="从 finance-ingest-cloud API 拉 pre-Router JSON，本地 Router/Rewriter（需 FINANCE_CLOUD_API_*）",
    )
    # 保留旧参数名以向后兼容
    parser.add_argument("--from-db", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    os.environ.update(_load_dotenv())

    if args.live_fetch and (_cloud_mode_enabled(args) or args.cloud):
        print(json.dumps({
            "ok": False,
            "error": {
                "code": "MUTUALLY_EXCLUSIVE_FLAGS",
                "message": "--live-fetch 与 --cloud / FINANCE_CLOUD_MODE 不能同时使用",
            },
        }, ensure_ascii=False, indent=2))
        sys.exit(1)

    if _cloud_mode_enabled(args):
        snap = _run_cloud_snapshot(args)
    elif args.live_fetch:
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

    if snap.get("error") and not snap.get("markdown_summary") and not snap.get("sections"):
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        sys.exit(0 if snap.get("ok") else 1)

    summary_only = bool(args.summary_only) and not args.full
    payload = _build_summary_view(snap) if summary_only else snap
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
