#!/usr/bin/env python3
"""Workspace 级快照缓存：读/写/失效（db_last_ingested_at 优先，6h 墙钟回退）。

与 ``query_market_facts.py``、``preflight_topic.py``、``query_direction_brief.py`` 共用。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platform_env import SNAPSHOT_CACHE_ENCODING_VERSION, cache_encoding_version_ok

SKILL_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SKILL_ROOT.parent.parent
DEFAULT_CACHE_SNAPSHOT = WORKSPACE_ROOT / "cache" / "snapshot" / "snapshot.json"
DEFAULT_CACHE_MARKDOWN = WORKSPACE_ROOT / "cache" / "snapshot" / "markdown_summary.md"
DEFAULT_MAX_AGE_HOURS = 6


def _load_dotenv() -> dict[str, str]:
    merged = dict(os.environ)
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


def _parse_iso_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        ts_str = ts.replace("Z", "+00:00")
        if "+" not in ts_str and "-" not in ts_str[-6:]:
            ts_str = ts_str + "+00:00"
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def snapshot_db_last_ingested_at(snapshot: dict[str, Any]) -> str:
    meta = snapshot.get("meta") or {}
    return str(meta.get("db_last_ingested_at") or meta.get("fetched_at") or "").strip()


def snapshot_fetched_at(snapshot: dict[str, Any]) -> str:
    return str((snapshot.get("meta") or {}).get("fetched_at") or "").strip()


def is_wall_clock_stale(fetched_at: str, max_age_hours: int) -> bool:
    dt = _parse_iso_ts(fetched_at)
    if dt is None:
        return True
    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return age_hours > max_age_hours


def fetch_remote_db_last_ingested_at(
    env: dict[str, str] | None = None,
    *,
    timeout_sec: int = 15,
) -> tuple[str | None, str | None]:
    """轻量探测云端库最后入库时间。返回 (db_last_utc, error_code)。"""
    merged = env if env is not None else _load_dotenv()
    base = (merged.get("FINANCE_CLOUD_API_BASE_URL") or "").strip().rstrip("/")
    key = (merged.get("FINANCE_CLOUD_API_KEY") or "").strip()
    if not base or not key:
        return None, "CLOUD_CONFIG_MISSING"
    url = f"{base}/api/v1/stats"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8")
        data = json.loads(body)
        meta = data.get("meta") or {}
        remote = str(meta.get("db_last_ingested_at_utc") or meta.get("db_last_ingested_at") or "").strip()
        return (remote or None), None
    except urllib.error.HTTPError as exc:
        return None, f"STATS_HTTP_{exc.code}"
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        return None, f"STATS_UNREACHABLE:{type(exc).__name__}"


def cache_stale_reason(
    cached: dict[str, Any],
    *,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    remote_db_last: str | None = None,
) -> str | None:
    """None = 仍新鲜；否则返回失效原因码。"""
    if not cached.get("ok"):
        return "cache_not_ok"
    if not (cached.get("markdown_summary") or cached.get("sections")):
        return "cache_empty"
    if not cache_encoding_version_ok(cached.get("meta") or {}):
        return "cache_encoding_version"
    try:
        from snapshot_text_encoding import looks_like_mojibake

        if looks_like_mojibake(str(cached.get("markdown_summary") or "")):
            return "markdown_mojibake"
    except Exception:
        pass
    local_db = snapshot_db_last_ingested_at(cached)
    fetched_at = snapshot_fetched_at(cached)
    if remote_db_last and local_db:
        if remote_db_last != local_db:
            return "db_last_ingested_at_changed"
        return None
    if is_wall_clock_stale(fetched_at, max_age_hours):
        return "max_age_hours"
    return None


def read_cache_file(cache_path: Path) -> dict[str, Any] | None:
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        from snapshot_text_encoding import ensure_snapshot_markdown, looks_like_mojibake

        data = ensure_snapshot_markdown(data)
        md = str(data.get("markdown_summary") or "")
        if md and looks_like_mojibake(md):
            return None
        return data
    except Exception:
        return None


def try_load_fresh_snapshot(
    cache_path: Path | None = None,
    *,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    check_remote_db: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """尝试加载仍新鲜的缓存。返回 (snapshot, info)。"""
    path = (cache_path or DEFAULT_CACHE_SNAPSHOT).resolve()
    info: dict[str, Any] = {
        "cache_path": str(path),
        "snapshot_cached": False,
        "cache_stale_reason": None,
        "remote_db_last_ingested_at": None,
        "remote_probe_error": None,
        "local_db_last_ingested_at": None,
        "snapshot_fetched_at": None,
    }
    cached = read_cache_file(path)
    if cached is None:
        info["cache_stale_reason"] = "cache_missing"
        return None, info

    info["local_db_last_ingested_at"] = snapshot_db_last_ingested_at(cached)
    info["snapshot_fetched_at"] = snapshot_fetched_at(cached)

    remote_db: str | None = None
    if check_remote_db:
        remote_db, probe_err = fetch_remote_db_last_ingested_at(env)
        info["remote_db_last_ingested_at"] = remote_db
        info["remote_probe_error"] = probe_err

    reason = cache_stale_reason(
        cached,
        max_age_hours=max_age_hours,
        remote_db_last=remote_db,
    )
    info["cache_stale_reason"] = reason
    if reason is None:
        info["snapshot_cached"] = True
        return cached, info
    return None, info


def write_snapshot_cache(
    snap: dict[str, Any],
    *,
    cache_path: Path | None = None,
) -> Path:
    from snapshot_text_encoding import ensure_snapshot_markdown

    path = (cache_path or DEFAULT_CACHE_SNAPSHOT).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    snap = ensure_snapshot_markdown(snap)
    md = str(snap.get("markdown_summary") or "")
    meta = dict(snap.get("meta") or {})
    meta["snapshot_cache_encoding_version"] = SNAPSHOT_CACHE_ENCODING_VERSION
    if md:
        md_path = path.parent / "markdown_summary.md"
        md_path.write_text(md, encoding="utf-8")
        meta["markdown_summary_sidecar"] = str(md_path.resolve())
    snap["meta"] = meta
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def should_write_cache(snap: dict[str, Any]) -> bool:
    if not snap.get("ok", True):
        return False
    if snap.get("error") and not snap.get("markdown_summary") and not snap.get("sections"):
        return False
    return bool(snap.get("markdown_summary") or snap.get("sections"))
