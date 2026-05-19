#!/usr/bin/env python3
"""带方向信息简报 — 读 workspace 快照缓存，按方向关键词过滤，不输出全量 markdown_summary。

用法：
  python3 query_direction_brief.py --direction '美伊局势和黄金'

stdout：单一 JSON（ok / direction_brief / snapshot_cached / source_gaps）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from preflight_topic import (
    _bullet_touches_direction,
    _expand_keywords,
    _markdown_bullet_lines,
    _relevance_score,
    _strict_direction_hints,
)
from snapshot_cache import (
    DEFAULT_CACHE_SNAPSHOT,
    DEFAULT_MAX_AGE_HOURS,
    WORKSPACE_ROOT,
    try_load_fresh_snapshot,
)

BRIEF_MAX_LINES = 18
BRIEF_LINE_CHARS = 220


def _sections_to_bullets(snapshot: dict[str, Any]) -> list[str]:
    md = str(snapshot.get("markdown_summary") or "")
    bullets = _markdown_bullet_lines(md, cap=64)
    if bullets:
        return bullets
    sections = snapshot.get("sections") or {}
    out: list[str] = []
    for key in ("market", "news", "social", "deep_news", "sectors"):
        block = sections.get(key)
        if isinstance(block, list):
            for item in block:
                if isinstance(item, str) and item.strip():
                    out.append(f"- {item.strip()[:BRIEF_LINE_CHARS]}")
                elif isinstance(item, dict):
                    title = str(item.get("title") or item.get("headline") or "").strip()
                    if title:
                        out.append(f"- {title[:BRIEF_LINE_CHARS]}")
        elif isinstance(block, dict):
            for sub in block.values():
                if isinstance(sub, list):
                    for item in sub:
                        if isinstance(item, dict):
                            title = str(item.get("title") or item.get("headline") or "").strip()
                            if title:
                                out.append(f"- {title[:BRIEF_LINE_CHARS]}")
    return out[:64]


def _rank_bullets(bullets: list[str], direction: str) -> list[dict[str, Any]]:
    hints = _strict_direction_hints(direction)
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in bullets:
        text = line.lstrip("- ").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        score = _relevance_score(text, direction)
        hint_hit = bool(hints and any(h in text for h in hints))
        touched = _bullet_touches_direction(line, direction)
        ranked.append(
            {
                "line": line[:BRIEF_LINE_CHARS],
                "score": round(score, 4),
                "direction_touch": touched,
                "domain_hint_hit": hint_hit,
            }
        )
    ranked.sort(
        key=lambda x: (
            0 if x["domain_hint_hit"] else 1,
            0 if x["direction_touch"] else 1,
            -float(x["score"]),
        )
    )
    return ranked


def build_direction_brief(
    direction: str,
    snapshot: dict[str, Any],
    *,
    max_lines: int = BRIEF_MAX_LINES,
) -> dict[str, Any]:
    kw, domain_tags, _kw_list = _expand_keywords(direction)
    bullets = _sections_to_bullets(snapshot)
    ranked = _rank_bullets(bullets, direction)
    picked = [x for x in ranked if x["direction_touch"] or x["domain_hint_hit"] or x["score"] >= 0.12]
    if len(picked) < 3:
        picked = ranked[:max_lines]
    else:
        picked = picked[:max_lines]

    source_gaps: list[str] = []
    if len(picked) < 2:
        source_gaps.append(
            "当前缓存快照中与该方向强相关的条目不足；可补充更具体关键词，或等待下一档入库后重试。"
        )

    lines = [x["line"] for x in picked]
    meta = snapshot.get("meta") or {}
    summary_parts = [
        f"【方向】{direction.strip()}",
        f"【检索关键词】{kw}",
        f"【领域标签】{', '.join(domain_tags) if domain_tags else '（无）'}",
        f"【数据截止】{meta.get('fetched_at') or '未知'}",
        "",
        "【相关事实摘要】",
    ]
    summary_parts.extend(lines if lines else ["（未命中同域事实，请勿脑补）"])

    return {
        "direction": direction.strip(),
        "keywords": kw,
        "domain_tags": domain_tags,
        "fact_lines": lines,
        "fact_count": len(lines),
        "markdown_brief": "\n".join(summary_parts),
        "source_gaps": source_gaps,
        "db_last_ingested_at": meta.get("db_last_ingested_at"),
        "usage_hint": (
            "向用户展示 markdown_brief（非全量快照）；若用户确认开稿，走 preflight_topic.py --direction。"
        ),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="带方向信息简报（读 cache，不贴全量 markdown_summary）")
    p.add_argument("--direction", required=True, help="用户自然语言方向")
    p.add_argument("--cache-snapshot-path", type=Path, default=DEFAULT_CACHE_SNAPSHOT)
    p.add_argument("--snapshot-max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS)
    p.add_argument("--max-lines", type=int, default=BRIEF_MAX_LINES)
    p.add_argument("--force-refresh", action="store_true", help="忽略 cache，先 force-refresh 拉全量再过滤")
    args = p.parse_args()

    direction = (args.direction or "").strip()
    if not direction:
        print(json.dumps({"ok": False, "error": {"code": "DIRECTION_EMPTY", "message": "direction 为空"}}, ensure_ascii=False))
        sys.exit(0)

    cache_info: dict[str, Any] = {}
    snapshot: dict[str, Any] | None = None

    if not args.force_refresh:
        snapshot, cache_info = try_load_fresh_snapshot(
            args.cache_snapshot_path,
            max_age_hours=int(args.snapshot_max_age_hours),
            check_remote_db=True,
        )

    if snapshot is None:
        import subprocess

        qmf = Path(__file__).resolve().parent / "query_market_facts.py"
        proc = subprocess.run(
            [sys.executable, str(qmf), "--sources", "market,news,social", "--full", "--force-refresh"],
            cwd=str(WORKSPACE_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "DIRECTION_BRIEF_SNAPSHOT_FAILED",
                            "message": "无法加载或刷新快照",
                            "hint": (proc.stderr or proc.stdout or "")[-600:],
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            sys.exit(0)
        snapshot = json.loads(proc.stdout)
        cache_info = {"snapshot_cached": False, "cache_stale_reason": "refreshed_for_brief"}

    brief = build_direction_brief(direction, snapshot, max_lines=int(args.max_lines))
    print(
        json.dumps(
            {
                "ok": True,
                "mode": "direction_brief",
                "snapshot_cached": bool(cache_info.get("snapshot_cached")),
                "cache_stale_reason": cache_info.get("cache_stale_reason"),
                "snapshot_path": str(args.cache_snapshot_path.resolve()),
                "direction_brief": brief,
                "hint_ok": brief.get("usage_hint"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
