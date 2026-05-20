#!/usr/bin/env python3
"""今日市场快照 — 默认云端 API；仅 --live-fetch 走联网 legacy。

- 默认：HTTP 拉 finance-ingest-cloud pre-Router JSON → 本地 db_snapshot Router/Rewriter
- --live-fetch：ingest.py legacy（实时 pipeline，慢，需网络）

云 API 不可用时直接报错，不自动降级 legacy。
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

from snapshot_cache import (
    DEFAULT_CACHE_MARKDOWN,
    DEFAULT_CACHE_SNAPSHOT,
    DEFAULT_MAX_AGE_HOURS,
    should_write_cache,
    try_load_fresh_snapshot,
    write_snapshot_cache,
)
from snapshot_text_encoding import ensure_snapshot_markdown
from platform_env import apply_python_utf8_mode, safe_update_os_environ

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


# ── 云路径（默认）────────────────────────────────────────────────────────────

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
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=db_timeout,
        check=False,
    )
    try:
        snap = _extract_json_object(proc.stdout or "")
        return ensure_snapshot_markdown(snap)
    except ValueError:
        return {
            "ok": False,
            "error": {
                "code": "CLOUD_SNAPSHOT_PARSE_ERROR",
                "message": "db_snapshot --pre-router-stdin 输出无法解析",
                "hint": (proc.stdout or "")[-500:],
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
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=getattr(args, "timeout", 120),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"finance-source-ingest legacy failed ({proc.returncode}): {(proc.stdout or '')[-2000:]}"
        )
    return _extract_json_object(proc.stdout or "")


def _build_summary_view(
    snap: dict[str, Any],
    *,
    cache_path: str | None = None,
    cache_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = snap.get("meta") or {}
    ci = cache_info or {}
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
            "snapshot_cache_path": cache_path or ci.get("cache_path"),
            "snapshot_cache_written": bool(cache_path),
            "snapshot_cached": bool(ci.get("snapshot_cached")),
            "cache_stale_reason": ci.get("cache_stale_reason"),
            "remote_db_last_ingested_at": ci.get("remote_db_last_ingested_at"),
        },
        "errors": snap.get("errors") or [],
        "markdown_summary": str(snap.get("markdown_summary") or ""),
        "markdown_summary_path": str(
            (snap.get("meta") or {}).get("markdown_summary_sidecar") or DEFAULT_CACHE_MARKDOWN
        ),
        "invariants": snap.get("invariants"),
    }


def main() -> None:
    apply_python_utf8_mode()
    parser = argparse.ArgumentParser(
        description="今日市场快照（默认云端 API；--live-fetch 为联网 legacy）",
    )
    parser.add_argument("--sources", default="market,news,social")
    parser.add_argument("--since-hours", type=int, default=24, help="云 API：新闻/板块主窗口（小时）")
    parser.add_argument("--major-since-hours", type=int, default=None, help="云 API：大事件窗口（默认 168）")
    parser.add_argument("--db-timeout", type=int, default=240, help="db_snapshot 子进程超时（秒）")
    parser.add_argument("--no-router", action="store_true", help="禁用 LLM Router")
    parser.add_argument("--no-rewrite", action="store_true", help="禁用板块润色")
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
        help="实时抓取（ingest.py legacy，需网络，慢）",
    )
    parser.add_argument(
        "--no-write-cache",
        action="store_true",
        help="成功时不写入 workspace-stream-gen/cache/snapshot/snapshot.json",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="跳过本地 cache，强制云 API + 本机 Router/Rewriter",
    )
    parser.add_argument(
        "--no-use-cache",
        action="store_true",
        help="同 --force-refresh",
    )
    parser.add_argument(
        "--snapshot-max-age-hours",
        type=int,
        default=DEFAULT_MAX_AGE_HOURS,
        help="cache 墙钟回退上限（小时）；db_last 探测成功时优先按库更新时间",
    )
    parser.add_argument(
        "--cache-snapshot-path",
        type=Path,
        default=DEFAULT_CACHE_SNAPSHOT,
        help="快照缓存路径（与 preflight 共用）",
    )
    parser.add_argument("--cloud", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--from-db", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    env = _load_dotenv()
    skipped_env = safe_update_os_environ(env)
    if skipped_env:
        print(
            "[query_market_facts] skipped long .env keys (Windows limit): "
            + ", ".join(skipped_env[:8]),
            file=sys.stderr,
        )

    cache_info: dict[str, Any] = {}
    snap: dict[str, Any]
    force_refresh = bool(args.force_refresh or args.no_use_cache)

    if args.live_fetch:
        try:
            snap = _run_live_fetch(args)
        except Exception as e:
            print(json.dumps({
                "ok": False,
                "error": {"code": "LIVE_FETCH_FAILED", "message": str(e)[:500]},
            }, ensure_ascii=False, indent=2))
            sys.exit(1)
    elif not force_refresh:
        cached, cache_info = try_load_fresh_snapshot(
            args.cache_snapshot_path,
            max_age_hours=int(args.snapshot_max_age_hours),
            check_remote_db=True,
            env=env,
        )
        if cached is not None:
            snap = cached
        else:
            snap = _run_cloud_snapshot(args)
    else:
        cache_info = {"cache_stale_reason": "force_refresh", "snapshot_cached": False}
        snap = _run_cloud_snapshot(args)

    if snap.get("error") and not snap.get("markdown_summary") and not snap.get("sections"):
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        sys.exit(0 if snap.get("ok") else 1)

    snap = ensure_snapshot_markdown(snap)

    cache_written: str | None = None
    if not args.no_write_cache and should_write_cache(snap) and not cache_info.get("snapshot_cached"):
        try:
            cache_written = str(write_snapshot_cache(snap, cache_path=args.cache_snapshot_path))
        except OSError:
            cache_written = None
    elif cache_info.get("snapshot_cached"):
        cache_written = str(args.cache_snapshot_path.resolve())

    summary_only = bool(args.summary_only) and not args.full
    if summary_only:
        payload = _build_summary_view(snap, cache_path=cache_written, cache_info=cache_info)
    elif cache_written or cache_info.get("snapshot_cached"):
        snap = dict(snap)
        meta = dict(snap.get("meta") or {})
        meta["snapshot_cache_path"] = cache_written or cache_info.get("cache_path")
        meta["snapshot_cache_written"] = bool(cache_written and not cache_info.get("snapshot_cached"))
        meta["snapshot_cached"] = bool(cache_info.get("snapshot_cached"))
        meta["cache_stale_reason"] = cache_info.get("cache_stale_reason")
        snap["meta"] = meta
        payload = snap
    else:
        payload = snap
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
