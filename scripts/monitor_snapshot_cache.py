#!/usr/bin/env python3
"""快照缓存监控：cache 新鲜度、warm 日志、cron 探测；可选模拟飞书全量拉数路径。

Usage:
  python3 scripts/monitor_snapshot_cache.py              # 状态 JSON
  python3 scripts/monitor_snapshot_cache.py --human     # 人类可读摘要
  python3 scripts/monitor_snapshot_cache.py --probe-feishu-full   # 模拟飞书「今日全量」
  python3 scripts/monitor_snapshot_cache.py --probe-feishu-full --force-refresh  # 强制云拉对照
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = WORKSPACE_ROOT / "skills" / "streamy-content-gen" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from snapshot_cache import (  # noqa: E402
    DEFAULT_CACHE_SNAPSHOT,
    DEFAULT_MAX_AGE_HOURS,
    fetch_remote_db_last_ingested_at,
    read_cache_file,
    snapshot_db_last_ingested_at,
    snapshot_fetched_at,
    try_load_fresh_snapshot,
)

WARM_LOG = Path(os.environ.get("FINANCE_SNAPSHOT_WARM_LOG", WORKSPACE_ROOT / "cache/snapshot/warm.log"))
MONITOR_LOG = Path(os.environ.get("FINANCE_SNAPSHOT_MONITOR_LOG", WORKSPACE_ROOT / "cache/snapshot/monitor.log"))
QMF = SCRIPTS_DIR / "query_market_facts.py"
PREFLIGHT = SCRIPTS_DIR / "preflight_topic.py"


def _file_mtime_iso(path: Path) -> str | None:
    if not path.is_file():
        return None
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _tail_lines(path: Path, n: int = 8) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-n:]


def _cron_installed() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        raw = proc.stdout or ""
    except Exception as exc:
        return {"installed": False, "error": str(exc)[:200]}
    has_block = "BEGIN stream-gen snapshot warm" in raw
    warm_path = str(WORKSPACE_ROOT / "scripts/warm_snapshot_cache.sh")
    path_ok = warm_path in raw
    return {
        "installed": has_block and path_ok,
        "has_block": has_block,
        "warm_script_path_matches": path_ok,
        "expected_warm_script": warm_path,
    }


def build_status(*, max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> dict[str, Any]:
    cache_path = DEFAULT_CACHE_SNAPSHOT.resolve()
    cached = read_cache_file(cache_path)
    fresh, cache_info = try_load_fresh_snapshot(cache_path, max_age_hours=max_age_hours, check_remote_db=True)

    warm_tail = _tail_lines(WARM_LOG)
    last_warm_ok = any("warm_snapshot_cache ok" in ln for ln in warm_tail[-3:])
    last_warm_fail = any("FAILED" in ln for ln in warm_tail[-3:])

    status = "ok"
    alerts: list[str] = []
    if not cache_path.is_file():
        status = "warn"
        alerts.append("cache_missing")
    elif not fresh:
        status = "warn"
        alerts.append(f"cache_stale:{cache_info.get('cache_stale_reason')}")
    if cache_info.get("remote_probe_error"):
        alerts.append(f"remote_stats:{cache_info.get('remote_probe_error')}")
    if last_warm_fail and not last_warm_ok:
        status = "warn"
        alerts.append("warm_recent_failure")

    return {
        "ok": status == "ok",
        "status": status,
        "alerts": alerts,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "workspace_root": str(WORKSPACE_ROOT),
        "cache": {
            "path": str(cache_path),
            "exists": cache_path.is_file(),
            "size_bytes": cache_path.stat().st_size if cache_path.is_file() else 0,
            "file_mtime_utc": _file_mtime_iso(cache_path),
            "local_db_last_ingested_at": cache_info.get("local_db_last_ingested_at"),
            "snapshot_fetched_at": cache_info.get("snapshot_fetched_at"),
            "would_hit_cache": bool(cache_info.get("snapshot_cached")),
            "stale_reason": cache_info.get("cache_stale_reason"),
        },
        "remote": {
            "db_last_ingested_at": cache_info.get("remote_db_last_ingested_at"),
            "probe_error": cache_info.get("remote_probe_error"),
        },
        "policy": {
            "max_age_hours_fallback": max_age_hours,
            "invalidation": "db_last_ingested_at first, then wall clock",
        },
        "warm_log": {
            "path": str(WARM_LOG),
            "tail": warm_tail,
            "recent_ok": last_warm_ok,
            "recent_failure": last_warm_fail,
        },
        "cron": _cron_installed(),
        "feishu_paths": {
            "full_snapshot": f"python3 {QMF} --sources market,news,social --summary-only",
            "direction_brief": f"python3 {SCRIPTS_DIR / 'query_direction_brief.py'} --direction '<方向>'",
            "preflight": f"python3 {PREFLIGHT} --direction '<方向>'",
        },
    }


def probe_feishu_full(*, force_refresh: bool = False, timeout_sec: int = 300) -> dict[str, Any]:
    """模拟飞书用户「今日行情/热点/全量信息」——与 AGENTS.md 一致。"""
    cmd = [
        sys.executable,
        str(QMF),
        "--sources",
        "market,news,social",
        "--summary-only",
    ]
    if force_refresh:
        cmd.append("--force-refresh")

    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(WORKSPACE_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    result: dict[str, Any] = {
        "probe": "feishu_full_snapshot",
        "command": " ".join(cmd),
        "exit_code": proc.returncode,
        "elapsed_ms": elapsed_ms,
        "force_refresh": force_refresh,
    }
    if proc.returncode != 0:
        result["ok"] = False
        result["stderr_tail"] = (proc.stderr or proc.stdout or "")[-800:]
        return result

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result["ok"] = False
        result["error"] = "stdout_not_json"
        result["stdout_tail"] = proc.stdout[-500:]
        return result

    meta = payload.get("meta") or {}
    result.update(
        {
            "ok": bool(payload.get("ok", True)),
            "snapshot_cached": bool(meta.get("snapshot_cached")),
            "cache_stale_reason": meta.get("cache_stale_reason"),
            "data_path": "cache" if meta.get("snapshot_cached") else "cloud_router",
            "fetched_at": meta.get("fetched_at"),
            "db_last_ingested_at": meta.get("db_last_ingested_at"),
            "markdown_chars": len(str(payload.get("markdown_summary") or "")),
        }
    )
    return result


def _human_report(status: dict[str, Any], probe: dict[str, Any] | None = None) -> str:
    lines = [
        "=== 快照缓存监控 ===",
        f"状态: {status.get('status')}  alerts={status.get('alerts') or '无'}",
        f"缓存文件: {status['cache']['path']}",
        f"  存在: {status['cache']['exists']}  大小: {status['cache']['size_bytes']} bytes",
        f"  本地 db_last: {status['cache'].get('local_db_last_ingested_at')}",
        f"  云端 db_last: {status['remote'].get('db_last_ingested_at')}  probe_err={status['remote'].get('probe_error')}",
        f"  下次全量会走 cache: {'是' if status['cache'].get('would_hit_cache') else '否'}  stale={status['cache'].get('stale_reason')}",
        f"cron 已装: {status['cron'].get('installed')}",
        f"warm.log 最近: ok={status['warm_log'].get('recent_ok')} fail={status['warm_log'].get('recent_failure')}",
    ]
    if probe:
        lines.extend(
            [
                "",
                "=== 飞书全量模拟探测 ===",
                f"路径: {probe.get('data_path')}  cached={probe.get('snapshot_cached')} 耗时={probe.get('elapsed_ms')}ms",
                f"force_refresh={probe.get('force_refresh')}  markdown_chars={probe.get('markdown_chars')}",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="快照 cache 监控与飞书路径探测")
    p.add_argument("--human", action="store_true", help="人类可读输出")
    p.add_argument("--probe-feishu-full", action="store_true", help="模拟飞书全量 query_market_facts")
    p.add_argument("--force-refresh", action="store_true", help="与 --probe-feishu-full 联用，强制云拉")
    p.add_argument("--append-log", action="store_true", help="追加一行 JSON 到 monitor.log")
    p.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS)
    args = p.parse_args()

    status = build_status(max_age_hours=int(args.max_age_hours))
    probe: dict[str, Any] | None = None
    if args.probe_feishu_full:
        probe = probe_feishu_full(force_refresh=bool(args.force_refresh))

    out = {"status": status, "probe": probe}

    if args.append_log:
        MONITOR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MONITOR_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(out, ensure_ascii=False) + "\n")

    if args.human:
        print(_human_report(status, probe))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
