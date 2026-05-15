#!/usr/bin/env python3
"""
finance-draft-manager — DB 快照构建器（v0.2.2）

从 finance_sources.db 读取时间窗内数据，输出与 pipeline.build_snapshot 兼容的
JSON 格式（含 sections + markdown_summary），供 preflight_topic.py / query_market_facts.py
直接消费。默认不抓取 RSS；可选接入 LLM Router、板块润色（与 legacy 同源脚本），
并对窗口内条目跑 `enhance_social_intelligence` 复现情绪量化块。

用法：
  db_snapshot.py [--since-hours 24] [--major-since-hours 168] [--db PATH] [--out-dir PATH]
                 [--keywords KW] [--sources market,news,social] [--no-router] [--no-rewrite]
                 [--summary-only]

stdout: 完整 snapshot JSON（--summary-only 时只含 meta/markdown_summary/errors/invariants）
--out-dir: 写入 snapshot.json 到该目录（供 preflight_topic.py 直接读取）

退出码：
  0  成功
  2  DB 不存在或无数据（json 内 ok=false）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
WORKSPACE_ROOT = SKILL_ROOT.parent.parent
INGEST_ROOT = WORKSPACE_ROOT / "skills" / "finance-source-ingest"
if str(INGEST_ROOT) not in sys.path:
    sys.path.insert(0, str(INGEST_ROOT))
_INGEST_SCRIPTS = INGEST_ROOT / "scripts"
if str(_INGEST_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_INGEST_SCRIPTS))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _load_dotenv() -> None:
    """加载 .env；**工作区** `WORKSPACE_ROOT/.env` 中的键覆盖已存在环境变量（便于覆盖根目录/Shell 默认值）。"""
    paths = (
        WORKSPACE_ROOT.parent / ".env",
        Path("/root/.openclaw/.env"),
        WORKSPACE_ROOT / ".env",
    )
    workspace_env = (WORKSPACE_ROOT / ".env").resolve()
    for path in paths:
        if not path.exists():
            continue
        force = path.resolve() == workspace_env
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key:
                continue
            if force or key not in os.environ:
                os.environ[key] = value


_load_dotenv()

from fetchers.sector_keywords import SECTOR_ORDER as _SK_ORDER, sectors_for_text  # noqa: E402
from fetchers.sentiment import extract_stock_mentions  # noqa: E402
from fetchers.social_intelligence import enhance_social_intelligence  # noqa: E402

from rewriter import RewriteResult, rewrite_sectors  # noqa: E402
from router import RouterResult, run_router  # noqa: E402

_SECTOR_ORDER: tuple[str, ...] = tuple(_SK_ORDER)
_SUMMARY_CLIP = 220
_MAJORS_KEYS = (
    "政策", "监管", "央行", "美联储", "地缘", "国务院", "战事", "冲突", "制裁", "关税",
    "降准", "降息", "CPI", "非农", "GDP", "商务部", "外交部", "霍尔木兹", "石油危机",
    "俄乌", "中东",
)
_POOL_PER_SECTOR = 28
_POOL_OTHER_FLASH = 72
_DISPLAY_PER_SECTOR = 6


def _default_major_hours() -> int:
    try:
        return max(24, int(os.environ.get("FINANCE_DB_SNAPSHOT_MAJOR_HOURS", "168")))
    except ValueError:
        return 168


def _env_truthy(name: str, default: str = "1") -> bool:
    v = os.environ.get(name, default).strip().lower()
    return v in ("1", "true", "yes", "on")


def _db_use_router_flag(cli_enabled: bool | None) -> bool:
    if cli_enabled is False:
        return False
    if cli_enabled is True:
        return True
    return _env_truthy("FINANCE_DB_SNAPSHOT_USE_ROUTER", "1")


def _db_use_rewrite_flag(cli_enabled: bool | None) -> bool:
    if cli_enabled is False:
        return False
    if cli_enabled is True:
        return True
    return _env_truthy("FINANCE_DB_SNAPSHOT_USE_REWRITE", "1")


def _item_key(it: dict[str, Any]) -> str:
    dk = str(it.get("dedupe_key") or "").strip()
    if dk:
        return dk
    return str(it.get("title") or "")[:120]


def _markdown_dedupe_fingerprint(it: dict[str, Any]) -> str:
    """跨「大事件 / 热点 / 深度」去重：优先 DB dedupe_key，否则标题+摘要前缀归一。"""
    dk = str(it.get("dedupe_key") or "").strip()
    if dk:
        return dk[:240]
    title = str(it.get("title") or "").strip().lower()
    title_c = re.sub(r"[\s\t\n\r]+", "", title)
    summ = str(it.get("clean_text") or it.get("summary") or "").strip().lower()
    summ_c = re.sub(r"[\s\t\n\r]+", "", summ)[:96]
    return f"{title_c}|{summ_c}"


def _dedupe_items_for_md(items: list[dict[str, Any]], seen: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        fp = _markdown_dedupe_fingerprint(it)
        if not fp or fp in seen:
            continue
        seen.add(fp)
        out.append(it)
    return out


def _rewrite_insight_usable(text: str) -> bool:
    """润色 insight 过短或占位（如 …）时回退板块洞察。"""
    s = (text or "").strip()
    if len(s) < 12:
        return False
    collapse = re.sub(r"[\s\u2026\u00b7\.。．·]+", "", s)
    if len(collapse) < 8:
        return False
    if s in ("...", "…", "……", "。。。"):
        return False
    if set(s) <= {".", "…", "。", "·", "．", " ", "\u2026"}:
        return False
    return True


def _rewrite_row_sentiment_label(ri: dict[str, Any], it: dict[str, Any]) -> str:
    """润色行前缀：优先模型 sentiment，与 impact/angle 对齐；缺省回退原条 sentiment_hint。"""
    s = str((ri or {}).get("sentiment") or "").strip()
    if s in ("利好", "利空", "中性"):
        return s
    if "利空" in s:
        return "利空"
    if "利好" in s:
        return "利好"
    h = str(it.get("sentiment_hint") or "中性")
    if "利空" in h:
        return "利空"
    if "利好" in h:
        return "利好"
    return "中性"


def _item_imp_sort(it: dict[str, Any]) -> tuple[float, str]:
    try:
        imp = float(it.get("importance_score") or 0.0)
    except (TypeError, ValueError):
        imp = 0.0
    return imp, str(it.get("published_at") or it.get("fetched_at") or "")


def _sector_tags_for_item(sec: str, it: dict[str, Any]) -> list[str]:
    tags = list(it.get("sector_tags") or [])
    tags_clean = [str(x).strip() for x in tags if str(x).strip()]
    if sec and sec not in tags_clean:
        tags_clean.insert(0, sec)
    return tags_clean


def _annotate_router_item(sec: str, it: dict[str, Any]) -> dict[str, Any]:
    out = dict(it)
    out["sector_tags"] = _sector_tags_for_item(sec, out)
    out["vertical_target_sector"] = sec
    out["candidate_sector"] = sec
    if not str(out.get("sector_line_source") or "").strip():
        out["sector_line_source"] = "llm_router"
    return out


def _deep_copy_item_for_intel(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(it.get("title") or "").strip(),
        "clean_text": str(it.get("clean_text") or it.get("summary") or "").strip(),
        "source": str(it.get("source") or it.get("source_name") or "").strip() or "unknown",
        "platform": str(it.get("source_name") or it.get("source") or "").strip() or "unknown",
    }


def _format_social_intel_md(social_intel: dict[str, Any]) -> tuple[list[str], bool]:
    """对齐 pipeline `_build_live_stream_markdown` 中「情绪量化指标」段落。"""
    agg_metrics = (social_intel or {}).get("aggregate_metrics") or {}
    bits: list[str] = []
    if not agg_metrics:
        return bits, False
    avg_sent = float(agg_metrics.get("headline_sentiment", agg_metrics.get("avg_sentiment", 0)) or 0)
    sent_label = agg_metrics.get("sentiment_label", "中性")
    fg_index = float(agg_metrics.get("fear_greed_index", 50) or 50)
    fg_label = agg_metrics.get("fear_greed_label", "中性")
    fg_emoji = agg_metrics.get("fear_greed_emoji", "⬜")
    reversal = agg_metrics.get("reversal_signal") or {}

    sent_emoji = "🟢" if avg_sent > 0.3 else "🟡" if avg_sent > 0 else "🟠" if avg_sent > -0.3 else "🔴"
    bits.append(f"**整体情绪分**：{avg_sent:.2f} ({sent_emoji} {sent_label})")
    bits.append(f"**恐惧贪婪指数**：{fg_index:.1f} ({fg_emoji} {fg_label})")

    if reversal.get("signal") != 0:
        rev_dir = reversal.get("direction", "")
        rev_reason = reversal.get("reason", "")
        rev_stren = float(reversal.get("strength", 0) or 0)
        rev_emoji = "⚠️" if rev_dir == "short" else "💡"
        bits.append(f"{rev_emoji} **信号提示**：{rev_reason}（强度：{rev_stren:.2f}）")

    stock_sents = (social_intel or {}).get("stock_sentiments") or {}
    if stock_sents:
        sorted_stocks = sorted(
            stock_sents.items(),
            key=lambda x: abs(float((x[1] or {}).get("avg_sentiment", 0) or 0)),
            reverse=True,
        )[:3]
        if sorted_stocks:
            stock_lines = []
            for stock, data in sorted_stocks:
                ss = float((data or {}).get("avg_sentiment", 0) or 0)
                se = "🟢" if ss > 0.2 else "🔴" if ss < -0.2 else "⚪"
                stock_lines.append(f"{stock}({se} {ss:.2f})")
            bits.append("**个股情绪极值**：" + "、".join(stock_lines))
    return bits, True


def _rewrite_meta_from_result(rw: RewriteResult | None) -> dict[str, Any]:
    if rw is None or not rw.by_sector:
        return {
            "status": "skipped",
            "status_by_sector": {},
            "timing_by_sector": {},
            "total_timing_sec": 0.0,
            "timeout_sec": None,
        }
    status_by_sec = {s: (rw.by_sector.get(s).status if rw.by_sector.get(s) else "missing") for s in _SECTOR_ORDER}
    timing_by_sec = {s: round(rw.by_sector[s].timing_sec, 3) for s in _SECTOR_ORDER if s in rw.by_sector}
    statuses = list(status_by_sec.values())
    if all(x in ("disabled", "not_configured") for x in statuses if x != "missing"):
        overall = "disabled"
    elif any(x == "ok" for x in statuses):
        overall = "partial" if any(x == "failed" for x in statuses) else "ok"
    else:
        overall = "failed"
    return {
        "status": overall,
        "status_by_sector": status_by_sec,
        "timing_by_sector": timing_by_sec,
        "total_timing_sec": rw.total_timing_sec,
        "timeout_sec": None,
    }


def _apply_router_rewrite_news(
    pool_news: dict[str, Any],
    *,
    deep_items: list[dict[str, Any]],
    use_router: bool,
    use_rewrite: bool,
    errors: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], RewriteResult | None, RouterResult | None]:
    """Router + 规则补位 + 可选 rewrite；返回 (news 更新, llm_router 元, rewrite 聚合, router_raw)。"""
    pool_by_sec: dict[str, list[dict[str, Any]]] = {
        s: list((pool_news.get("items_by_sector") or {}).get(s) or []) for s in _SECTOR_ORDER
    }
    other_pool: list[dict[str, Any]] = list(pool_news.get("items_other_flash") or [])

    def _fallback_news_trim() -> dict[str, Any]:
        by_sec = {s: sorted(pool_by_sec[s], key=_item_imp_sort, reverse=True)[:_DISPLAY_PER_SECTOR] for s in _SECTOR_ORDER}
        other = sorted(other_pool, key=_item_imp_sort, reverse=True)[:40]
        flat: list[dict[str, Any]] = []
        for s in _SECTOR_ORDER:
            flat.extend(by_sec[s])
        flat.extend(other)
        return {
            **pool_news,
            "items_by_sector": by_sec,
            "items_other_flash": other,
            "items": flat,
        }

    router_result: RouterResult | None = None
    insights_by_sec: dict[str, str] = {s: "" for s in _SECTOR_ORDER}
    final_by_sec: dict[str, list[dict[str, Any]]] = {s: [] for s in _SECTOR_ORDER}
    llm_router_payload: dict[str, Any] = {
        "status": "db_grouped",
        "items_by_sector": {},
        "menu_count": 0,
        "selected_count": 0,
        "reason": "",
        "insights_by_sector": insights_by_sec,
        "candidate_diagnostics": {},
        "router_timing": {},
    }

    menu_per_sec = max(3, min(25, int(os.environ.get("FINANCE_LLM_ROUTER_MENU_PER_SECTOR", "8"))))

    candidates: list[dict[str, Any]] = []
    for sec in _SECTOR_ORDER:
        for it in pool_by_sec[sec][: min(menu_per_sec, len(pool_by_sec[sec]))]:
            c = {
                **it,
                "sector": sec,
                "clean_title": it.get("title") or "",
                "raw_title": it.get("title") or "",
            }
            candidates.append(c)

    menu_count = len(candidates)
    llm_router_payload["menu_count"] = menu_count

    def _run_rewrite_on(news: dict[str, Any]) -> RewriteResult | None:
        fin = news.get("items_by_sector") or {}
        rw_input = {
            sec: [
                {
                    "clean_title": str(x.get("title") or ""),
                    "raw_title": str(x.get("title") or ""),
                    "clean_summary": str(x.get("clean_text") or x.get("summary") or "")[:400],
                    "sentiment_hint": str(x.get("sentiment_hint") or "中性"),
                }
                for x in (fin.get(sec) or [])[:3]
            ]
            for sec in _SECTOR_ORDER
            if fin.get(sec)
        }
        if not rw_input:
            return None
        return rewrite_sectors(rw_input)

    if not use_router or not candidates:
        router_result = RouterResult(status="skipped")
        news_out = _fallback_news_trim()
        llm_router_payload["items_by_sector"] = news_out["items_by_sector"]
        llm_router_payload["selected_count"] = len(news_out.get("items") or [])
        llm_router_payload["reason"] = "router_skipped_or_empty"
        llm_router_payload["status"] = "skipped" if not use_router else "no_candidates"
        rw_res = _run_rewrite_on(news_out) if use_rewrite else None
        return news_out, llm_router_payload, rw_res, router_result

    router_result = run_router(candidates)
    llm_router_payload["router_timing"] = {"total_sec": router_result.timing_sec}
    id_to_cand = {int(c.get("_router_id") or 0): c for c in candidates if c.get("_router_id")}

    if router_result.status != "ok":
        if router_result.status == "not_configured":
            llm_router_payload["status"] = "not_configured"
            llm_router_payload["reason"] = router_result.error or "router_not_configured"
        else:
            llm_router_payload["status"] = "fallback_grouped"
            llm_router_payload["reason"] = router_result.error or "router_failed"
            errors.append({
                "source": "llm_router",
                "code": "LLM_ROUTER_FAILED",
                "message": router_result.error[:500] if router_result.error else router_result.status,
            })
        news_out = _fallback_news_trim()
        llm_router_payload["items_by_sector"] = news_out["items_by_sector"]
        llm_router_payload["insights_by_sector"] = dict(insights_by_sec)
        llm_router_payload["selected_count"] = len(news_out.get("items") or [])
        rw_res = _run_rewrite_on(news_out) if use_rewrite else None
        return news_out, llm_router_payload, rw_res, router_result

    llm_router_payload["status"] = "ok"
    insights_by_sec = dict(router_result.insight_by_sector or {})
    llm_router_payload["insights_by_sector"] = insights_by_sec

    # 先按 Router ID 顺序落地，再按 importance 在同板块池内补位
    for sec in _SECTOR_ORDER:
        pool = list(pool_by_sec[sec])
        pool_sorted = sorted(pool, key=_item_imp_sort, reverse=True)
        picked: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rid in router_result.ids_by_sector.get(sec) or []:
            try:
                idx = int(rid)
            except (TypeError, ValueError):
                continue
            cand = id_to_cand.get(idx)
            if not cand:
                continue
            if str(cand.get("sector") or "") != sec:
                continue
            raw = {k: v for k, v in cand.items() if k not in ("sector", "clean_title", "raw_title", "_router_id")}
            k = _item_key(raw)
            if not k or k in seen:
                continue
            picked.append(_annotate_router_item(sec, raw))
            seen.add(k)
        for it in pool_sorted:
            if len(picked) >= _DISPLAY_PER_SECTOR:
                break
            k = _item_key(it)
            if not k or k in seen:
                continue
            picked.append(_annotate_router_item(sec, dict(it)))
            seen.add(k)
        # 仍偏少：从深度稿补位
        if len(picked) < 2 and deep_items:
            for d in sorted(deep_items, key=_item_imp_sort, reverse=True):
                if len(picked) >= _DISPLAY_PER_SECTOR:
                    break
                psec = str(d.get("primary_sector") or "").strip()
                blob = f"{d.get('title','')} {d.get('clean_text','')}"
                infer = sectors_for_text(blob)
                if sec != psec and sec not in infer:
                    continue
                k = _item_key(d)
                if not k or k in seen:
                    continue
                dd = dict(d)
                dd["sector_line_source"] = "deep_news"
                picked.append(_annotate_router_item(sec, dd))
                seen.add(k)
        final_by_sec[sec] = picked[:_DISPLAY_PER_SECTOR]

    # other_flash：未进六大桶的仍从原池来
    other_trim = sorted(other_pool, key=_item_imp_sort, reverse=True)[:40]
    flat: list[dict[str, Any]] = []
    for s in _SECTOR_ORDER:
        flat.extend(final_by_sec[s])
    flat.extend(other_trim)

    news_out = {
        **pool_news,
        "items_by_sector": final_by_sec,
        "items_other_flash": other_trim,
        "items": flat,
    }
    llm_router_payload["items_by_sector"] = final_by_sec
    llm_router_payload["selected_count"] = sum(len(final_by_sec[s]) for s in _SECTOR_ORDER) + len(other_trim)

    rw_res = _run_rewrite_on(news_out) if use_rewrite else None
    return news_out, llm_router_payload, rw_res, router_result


# （_load_dotenv 已在模块首部调用）


def _resolve_db(db_arg: str = "") -> Path:
    if db_arg:
        return Path(db_arg)
    env = os.environ.get("FINANCE_DB_PATH", "").strip()
    if env:
        return Path(env)
    return WORKSPACE_ROOT / "user_data" / "finance_sources.db"


def _connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _cutoff(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── DB 读取层 ─────────────────────────────────────────────────────────────────

def _read_market(conn: sqlite3.Connection, since_hours: int) -> dict[str, Any]:
    """读 market_snapshots，返回兼容 pipeline sections['market'] 的结构。"""
    cutoff = _cutoff(since_hours)
    cur = conn.execute(
        """SELECT * FROM market_snapshots
           WHERE snapshot_at >= ?
           ORDER BY snapshot_at DESC""",
        (cutoff,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return {}

    # 取每个 index_code 最新一条
    seen: dict[str, dict] = {}
    for r in rows:
        code = str(r.get("index_code") or "")
        if code and code not in seen:
            seen[code] = r

    items = []
    for r in seen.values():
        payload = {}
        raw = r.get("raw_payload_json") or "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            pass
        items.append({
            "code": r.get("index_code") or "",
            "name": r.get("index_name") or payload.get("name") or "",
            "close": r.get("price"),
            "pct_change": r.get("change_pct"),
        })

    return {
        "source_primary": "db",
        "as_of": rows[0].get("snapshot_at") if rows else _now_iso(),
        "a_share_indices": {
            "source": "db:market_snapshots",
            "items": items,
        },
    }


def _news_title(r: dict) -> str:
    return str(r.get("clean_title") or r.get("raw_title") or "").strip()


def _news_summary(r: dict) -> str:
    return str(r.get("clean_summary") or r.get("raw_content") or "").strip()[:500]


def _news_row_to_item(r: dict, primary_sector: str) -> dict[str, Any]:
    title = _news_title(r)
    summary = _news_summary(r)
    summ_short = summary[:_SUMMARY_CLIP] if summary else ""
    try:
        imp = float(r.get("importance_score") or 0.0)
    except (TypeError, ValueError):
        imp = 0.0
    sent = str(r.get("sentiment") or "").strip() or "中性"
    return {
        "dedupe_key": str(r.get("dedupe_key") or ""),
        "title": title,
        "clean_text": summ_short,
        "summary": summ_short,
        "source": r.get("source") or "",
        "source_name": r.get("source") or "",
        "url": r.get("source_url") or "",
        "published_at": r.get("published_at") or r.get("fetched_at") or "",
        "sentiment_hint": sent,
        "importance_score": imp,
        "primary_sector": primary_sector,
        "llm_clean_status": r.get("llm_clean_status") or "",
    }


def _read_news_by_sector(
    conn: sqlite3.Connection,
    since_hours: int,
    keywords: list[str],
) -> dict[str, Any]:
    """
    读 news_items，按板块分组。优先使用入库/清洗的 sector；缺失时用 sector_keywords 从标题+摘要推断。
    返回「池化」结果供后续 Router + 规则补位（本函数本身不调 LLM）。
    """
    cutoff = _cutoff(since_hours)
    cur = conn.execute(
        """SELECT dedupe_key, source, source_url,
                  raw_title, raw_content,
                  clean_title, clean_summary,
                  sector, sentiment, importance_score, tags_json,
                  published_at, fetched_at, llm_clean_status
           FROM news_items
           WHERE fetched_at >= ?
           ORDER BY importance_score DESC, fetched_at DESC
           LIMIT 800""",
        (cutoff,),
    )
    all_rows = [dict(r) for r in cur.fetchall()]

    if keywords:
        filtered = []
        for r in all_rows:
            text = f"{_news_title(r)} {_news_summary(r)}"
            if any(k in text for k in keywords):
                filtered.append(r)
        all_rows = filtered if filtered else all_rows

    by_sector: dict[str, list[dict[str, Any]]] = {s: [] for s in _SECTOR_ORDER}
    other_flash: list[dict[str, Any]] = []

    for r in all_rows:
        title = _news_title(r)
        summary = _news_summary(r)
        if not title:
            continue
        stored = str(r.get("sector") or "").strip()
        blob = f"{title} {summary}"
        if stored in _SECTOR_ORDER:
            primary = stored
        else:
            inferred = sectors_for_text(blob)
            primary = inferred[0] if inferred else ""

        item = _news_row_to_item(r, primary)
        if primary in by_sector:
            by_sector[primary].append(item)
        else:
            other_flash.append(item)

    def _sort_trim(bucket: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
        bucket.sort(key=lambda x: (float(x.get("importance_score") or 0.0), str(x.get("published_at") or "")), reverse=True)
        return bucket[:cap]

    per_pool = _POOL_PER_SECTOR
    for sec in _SECTOR_ORDER:
        by_sector[sec] = _sort_trim(by_sector[sec], per_pool)

    other_flash = _sort_trim(other_flash, _POOL_OTHER_FLASH)

    flat: list[dict[str, Any]] = []
    for sec in _SECTOR_ORDER:
        for it in by_sector[sec]:
            flat.append(it)
    flat.extend(other_flash)

    return {
        "source_primary": "db",
        "as_of": _now_iso(),
        "items_by_sector": by_sector,
        "items_other_flash": other_flash,
        "items": flat,
    }


def _read_deep_news(
    conn: sqlite3.Connection,
    since_hours: int,
) -> dict[str, Any]:
    """高 importance_score 条目作为深度资讯区（阈值略低于旧版单行逻辑，保证条数）。"""
    cutoff = _cutoff(since_hours)
    cur = conn.execute(
        """SELECT source, source_url, dedupe_key,
                  raw_title, clean_title, clean_summary, raw_content,
                  sector, sentiment, importance_score, published_at, fetched_at
           FROM news_items
           WHERE fetched_at >= ? AND COALESCE(importance_score,0) >= 0.35
           ORDER BY importance_score DESC, fetched_at DESC
           LIMIT 15""",
        (cutoff,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    items = []
    for r in rows:
        title = str(r.get("clean_title") or r.get("raw_title") or "").strip()
        if not title:
            continue
        body = str(r.get("clean_summary") or r.get("raw_content") or "").strip()
        items.append({
            "title": title,
            "summary": body[:_SUMMARY_CLIP],
            "clean_text": body[:_SUMMARY_CLIP],
            "url": r.get("source_url") or "",
            "published_at": r.get("published_at") or r.get("fetched_at") or "",
            "source": r.get("source") or "",
            "source_name": r.get("source") or "",
            "sector_tags": [r.get("sector")] if r.get("sector") else [],
            "sentiment_hint": r.get("sentiment") or "中性",
            "importance_score": float(r.get("importance_score") or 0),
            "dedupe_key": str(r.get("dedupe_key") or ""),
            "primary_sector": str(r.get("sector") or "").strip(),
        })
    return {"items": items, "sources_ok": ["db"] if items else []}


def _read_major_event_items(
    conn: sqlite3.Connection,
    major_hours: int,
    keywords: list[str],
) -> list[dict[str, Any]]:
    """更长窗口内命中大事件关键词的条目（用于「大事件」栏，不影响六大板块主池）。"""
    cutoff = _cutoff(major_hours)
    cur = conn.execute(
        """SELECT dedupe_key, source, source_url,
                  raw_title, raw_content,
                  clean_title, clean_summary,
                  sector, sentiment, importance_score, tags_json,
                  published_at, fetched_at, llm_clean_status
           FROM news_items
           WHERE fetched_at >= ?
           ORDER BY importance_score DESC, fetched_at DESC
           LIMIT 600""",
        (cutoff,),
    )
    rows = [dict(r) for r in cur.fetchall()]

    def _title(r: dict) -> str:
        return _news_title(r)

    def _summary(r: dict) -> str:
        return _news_summary(r)

    if keywords:
        filtered = []
        for r in rows:
            text = f"{_title(r)} {_summary(r)}"
            if any(k in text for k in keywords):
                filtered.append(r)
        rows = filtered if filtered else rows

    majors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        title = _title(r)
        summary = _summary(r)
        if not title:
            continue
        stored = str(r.get("sector") or "").strip()
        blob = f"{title} {summary}"
        if stored in _SECTOR_ORDER:
            primary = stored
        else:
            inferred = sectors_for_text(blob)
            primary = inferred[0] if inferred else ""
        if not _is_major_blob(title, summary, primary or stored):
            continue
        it = _news_row_to_item(r, primary or stored)
        k = _item_key(it)
        if not k or k in seen:
            continue
        seen.add(k)
        majors.append(it)
        if len(majors) >= 14:
            break

    majors.sort(key=_item_imp_sort, reverse=True)
    return majors[:12]


def _read_sentiment(conn: sqlite3.Connection, since_hours: int) -> dict[str, Any]:
    """读 sentiment_hot 作为 sections['social']。"""
    cutoff = _cutoff(since_hours)
    cur = conn.execute(
        """SELECT source, keyword, rank, heat, sector, related_stock
           FROM sentiment_hot
           WHERE snapshot_at >= ?
           ORDER BY snapshot_at DESC, rank ASC
           LIMIT 30""",
        (cutoff,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    items = []
    for r in rows:
        kw = str(r.get("keyword") or "").strip()
        if not kw:
            continue
        items.append({
            "title": kw,
            "keyword": kw,
            "clean_text": kw,
            "heat": str(r.get("heat") or ""),
            "platform": str(r.get("source") or ""),
            "source_name": str(r.get("source") or ""),
        })
    return {"items": items, "source_primary": "db"}


def _dedupe_intel_items(chunks: list[dict[str, Any]], cap: int = 400) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in chunks:
        if not isinstance(it, dict):
            continue
        cp = _deep_copy_item_for_intel(it)
        txt = f"{cp.get('title') or ''} {cp.get('clean_text') or ''}"
        if txt.strip():
            cp["stock_mentions"] = extract_stock_mentions(txt)
        k = (cp.get("title") or "")[:80] + "|" + str(cp.get("source") or "")
        if k in seen:
            continue
        seen.add(k)
        out.append(cp)
        if len(out) >= cap:
            break
    return out


def _compute_social_intelligence(
    sections: dict[str, Any],
    errors: list[dict[str, Any]],
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """聚合新闻 / Router 结果 / 深度 / sentiment_hot，跑与 ingest pipeline 同源的情绪量化。"""
    news_sec = sections.get("news") or {}
    llm = sections.get("llm_router") or {}
    deep = sections.get("deep_news") or {}
    social = sections.get("social") or {}
    chunks: list[dict[str, Any]] = []
    for sec in _SECTOR_ORDER:
        chunks.extend((news_sec.get("items_by_sector") or {}).get(sec) or [])
    chunks.extend(news_sec.get("items_other_flash") or [])
    ritems = llm.get("items_by_sector") or {}
    if isinstance(ritems, dict):
        for sec in _SECTOR_ORDER:
            chunks.extend(ritems.get(sec) or [])
    chunks.extend(deep.get("items") or [])
    chunks.extend(social.get("items") or [])
    deduped = _dedupe_intel_items(chunks, cap=400)
    if len(deduped) < 2:
        return None
    hist_run_s: list[float] = []
    hist_run_b: list[float] = []
    hist_fg: list[float] = []
    if conn is not None and _env_truthy("FINANCE_SOCIAL_INTEL_HISTORY_ENABLED", "1"):
        try:
            from storage import fetch_social_intel_run_history

            lim = max(1, int(os.environ.get("FINANCE_SOCIAL_INTEL_HISTORY_RUNS", "30")))
            rows = fetch_social_intel_run_history(conn, lim)
            for r in rows:
                hist_run_s.append(float(r["headline_sentiment"]))
                hist_run_b.append(float(r["mean_buzz_score"]))
                hist_fg.append(float(r["fear_greed_index"]))
        except Exception as exc:  # noqa: BLE001
            errors.append({
                "source": "social_intelligence",
                "code": "SOCIAL_INTEL_HISTORY_LOAD_FAILED",
                "message": str(exc)[:400],
            })
    try:
        return enhance_social_intelligence(
            deduped,
            historical_fg=hist_fg if hist_fg else None,
            historical_run_sentiments=hist_run_s if hist_run_s else None,
            historical_run_buzz=hist_run_b if hist_run_b else None,
        )
    except Exception as exc:  # noqa: BLE001
        errors.append({
            "source": "social_intelligence",
            "code": "ENHANCE_FAILED",
            "message": str(exc)[:400],
        })
        return None


# ── Markdown（对齐 legacy 章节 + Top3；DB 用关键词补板块）──────────────────────────

def _sent_prefix(hint: str) -> str:
    h = (hint or "").strip() or "中性"
    if "利好" in h:
        return "🟢【利好】"
    if "利空" in h:
        return "🔴【利空】"
    return "⚪【中性】"


def _line_body(it: dict[str, Any]) -> str:
    t = str(it.get("title") or "").strip()
    s = str(it.get("clean_text") or it.get("summary") or "").strip()
    if s and s != t:
        return f"{t}：{s}"
    return t


def _is_major_blob(title: str, summary: str, sector: str) -> bool:
    blob = f"{title} {summary} {sector}"
    return any(k in blob for k in _MAJORS_KEYS) or sector.strip() in ("宏观", "政策")


def _candidates_for_top3(news: dict[str, Any], deep: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def add_from(items: list[dict[str, Any]]) -> None:
        for it in items:
            if not isinstance(it, dict):
                continue
            k = str(it.get("dedupe_key") or "") or str(it.get("title") or "")[:80]
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(it)

    for sec in _SECTOR_ORDER:
        add_from((news.get("items_by_sector") or {}).get(sec) or [])
    add_from(news.get("items_other_flash") or [])
    add_from((deep or {}).get("items") or [])

    out.sort(key=lambda x: float(x.get("importance_score") or 0.0), reverse=True)
    return out


def _build_top3_lines(candidates: list[dict[str, Any]], limit: int = 3) -> list[str]:
    lines: list[str] = []
    used_sec: set[str] = set()
    picked: list[dict[str, Any]] = []

    for it in candidates:
        if len(picked) >= limit:
            break
        sec = str(it.get("primary_sector") or "").strip()
        tags = it.get("sector_tags") or []
        if isinstance(tags, list) and tags and not sec:
            sec = str(tags[0] or "")
        if sec in used_sec and len(used_sec) < limit:
            continue
        if sec:
            used_sec.add(sec)
        picked.append(it)

    if len(picked) < limit:
        for it in candidates:
            if it in picked:
                continue
            picked.append(it)
            if len(picked) >= limit:
                break

    if not picked:
        return ["- （本轮未筛出足够清晰的开稿主线；可看上方六大板块与热点。）"]

    for rank, it in enumerate(picked, start=1):
        title = str(it.get("title") or "").strip()
        summ = str(it.get("clean_text") or it.get("summary") or "").strip()
        sent_raw = str(it.get("sentiment_hint") or "中性")
        sp = _sent_prefix(sent_raw)
        src = str(it.get("source_name") or it.get("source") or "").strip()
        sec_lbl = str(it.get("primary_sector") or "").strip()
        if not sec_lbl and isinstance(it.get("sector_tags"), list) and it["sector_tags"]:
            sec_lbl = str(it["sector_tags"][0] or "")
        head = f"{title}" + (f"（{sec_lbl}）" if sec_lbl else "")
        reason_bits = []
        if summ:
            reason_bits.append(summ[:120] + ("…" if len(summ) > 120 else ""))
        reason_bits.append(f"情绪：{sent_raw}")
        reason = "；".join(reason_bits)
        anchor = (f"来源：{src}；" if src else "") + (summ[:140] if summ else title[:140])
        lines.append(f"**Top {rank}｜标题方向**：{head}")
        lines.append(f"- 为什么值得写：{reason}")
        lines.append(f"- 事实锚点：{anchor}")
    return lines


# ── Markdown 构建 ─────────────────────────────────────────────────────────────

def _build_markdown(
    sections: dict[str, Any],
    db_path: str,
    db_last_at: str,
    since_hours: int,
    major_since_hours: int,
) -> str:
    lines: list[str] = []
    now_cn = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

    lines.append(
        f"## 📊 今日信源全量快照（数据库 · 板块/快讯 {since_hours}h · 大事件 {major_since_hours}h · {now_cn} CST）"
    )
    disp_last = db_last_at[:19].replace("T", " ") if len(db_last_at) >= 16 else (db_last_at or "未知")
    lines.append(f"> 数据源：`{db_path}`　最后入库：{disp_last}（新闻与市场快照时间的较大值）")
    lines.append(
        "> 💡 **数据引擎**：DB 离线快照；六大板块 = **LLM Router（可关）+ 规则补位 + 可选小模型润色**；"
        "情绪量化 = 离线 `enhance_social_intelligence`（与 ingest 同源）。"
        "扩展行情（北向、行业强弱等）需更全 `market_snapshots` 或使用 `--live-fetch`。 "
        "环境变量：`FINANCE_DB_SNAPSHOT_USE_ROUTER` / `FINANCE_DB_SNAPSHOT_USE_REWRITE` / `FINANCE_DB_SNAPSHOT_MAJOR_HOURS`。"
    )
    lines.append("")

    m = sections.get("market") or {}
    idx = (m.get("a_share_indices") or {}).get("items") or []
    idx_parts: list[str] = []
    for x in idx:
        nm = str(x.get("name") or "").strip()
        cl = x.get("close") or x.get("price")
        pc = x.get("pct_change") or x.get("change_pct")
        if nm and cl is not None and isinstance(pc, (int, float)):
            sign = "+" if pc >= 0 else ""
            idx_parts.append(f"**{nm}** {cl} ({sign}{pc:.2f}%)")
        elif nm and cl is not None:
            idx_parts.append(f"**{nm}** {cl}")
    lines.append("### 【📈 大盘与情绪】")
    if idx_parts:
        lines.append("- **三大指数**：" + " / ".join(idx_parts))
        lines.append("- （若仅有指数点位：北向/涨跌停等见定时采集扩展字段或 legacy 全量。）")
    else:
        lines.append("- 暂无指数快照（`market_snapshots` 在该窗口为空）。")
    lines.append("")

    si = sections.get("social_intelligence") or {}
    si_lines, ok_si = _format_social_intel_md(si)
    lines.append("### 【🧠 情绪量化指标】（离线聚合 · DB 窗口）")
    if ok_si and si_lines:
        for bit in si_lines:
            lines.append(f"- {bit}")
    else:
        lines.append("- （条目不足或未算出聚合指标；可检查近窗新闻是否入库。）")
    lines.append("")

    news = sections.get("news") or {}
    by_sec = news.get("items_by_sector") or {}
    llm = sections.get("llm_router") or {}
    insights = llm.get("insights_by_sector") or {}
    rw_block = sections.get("sector_llm_rewrite") or {}
    rw_disp = rw_block.get("display") or {}

    lines.append("### 【🎯 核心板块异动】（六大板块 · Router / 润色 / 情绪）")
    has_sector = False
    for sec in _SECTOR_ORDER:
        items = by_sec.get(sec) or []
        if not items:
            continue
        has_sector = True
        lines.append(f"\n**{sec}**")
        ins = str(insights.get(sec) or "").strip()
        if ins:
            lines.append(f"- *板块洞察*：{ins}")
        dins = rw_disp.get(sec) if isinstance(rw_disp.get(sec), dict) else {}
        rw_ok = str(dins.get("status") or "") == "ok"
        rw_ins = str(dins.get("insight") or "").strip()
        if rw_ok and rw_ins:
            if _rewrite_insight_usable(rw_ins):
                lines.append(f"- *润色洞察*：{rw_ins}")
            elif ins:
                lines.append(f"- *润色洞察*：{ins}")
        rw_items = dins.get("items") if isinstance(dins.get("items"), list) else []
        for i, it in enumerate(items[:_DISPLAY_PER_SECTOR]):
            if i < len(rw_items) and rw_ok:
                ri = rw_items[i]
                if isinstance(ri, dict):
                    t = str(ri.get("title") or it.get("title") or "")
                    imp = str(ri.get("impact") or "")
                    ang = str(ri.get("angle") or "")
                    sl = _rewrite_row_sentiment_label(ri, it)
                    lines.append(
                        f"- 🧱【润色】{_sent_prefix(sl)}"
                        f"**{t}** 影响：{imp} 角度：{ang}"
                    )
                    continue
            lines.append(f"- {_sent_prefix(str(it.get('sentiment_hint') or '中性'))}{_line_body(it)}")
    if not has_sector:
        lines.append("- （六大板块暂无命中；请看下方热点与深度区。）")
    lines.append("")

    md_dedupe_seen: set[str] = set()
    major_items = (sections.get("major_events") or {}).get("items") or []
    lines.append(f"### 【🧭 大事件】（政策/宏观/地缘等 · 近 {major_since_hours}h）")
    if major_items:
        major_for_md = _dedupe_items_for_md(major_items, md_dedupe_seen)[:10]
        for it in major_for_md:
            lines.append(f"- {_sent_prefix(str(it.get('sentiment_hint') or '中性'))}{_line_body(it)}")
    else:
        lines.append("- （该窗口未命中大事件关键词；可从板块与 Top3 择题。）")
    lines.append("")

    other = news.get("items_other_flash") or []
    lines.append("### 【🔥 今日热点讯息】（未归入六大专属桶 · 精选）")
    if other:
        other_for_md = _dedupe_items_for_md(other, md_dedupe_seen)[:20]
        if other_for_md:
            for it in other_for_md:
                lines.append(f"- {_sent_prefix(str(it.get('sentiment_hint') or '中性'))}{_line_body(it)}")
        else:
            lines.append("- （与上方章节去重后暂无额外热点条目。）")
    else:
        lines.append("- （热点已并入六大板块。）")
    lines.append("")

    deep = (sections.get("deep_news") or {}).get("items") or []
    if deep:
        deep_for_md = _dedupe_items_for_md(deep, md_dedupe_seen)[:12]
        if deep_for_md:
            lines.append("### 【📌 深度资讯】（importance≥0.35）")
            for it in deep_for_md:
                lines.append(f"- {_sent_prefix(str(it.get('sentiment_hint') or '中性'))}{_line_body(it)}")
            lines.append("")

    social_items = (sections.get("social") or {}).get("items") or []
    lines.append("**社媒 / 人气榜（探测 · DB）**")
    if social_items:
        kws = [str(it.get("keyword") or it.get("title") or "").strip() for it in social_items[:15] if it]
        kws = [k for k in kws if k]
        if kws:
            lines.append("- 热搜词：" + "、".join(kws))
    else:
        lines.append("- 暂无 `sentiment_hot` 数据（`ingest.py run` 含 social 时会写入）。")
    lines.append("")

    top_cand = _candidates_for_top3(news, sections.get("deep_news") or {})
    lines.append("### 【✍️ 今日值得开稿 Top 3】（选题雷达 · importance + 板块分散）")
    lines.extend(_build_top3_lines(top_cand, limit=3))
    lines.append("")

    lines.append(
        "> ⚡ 本地数据库快照，无实时行情请求。要与旧版全文 1:1 可用：`python skills/streamy-content-gen/scripts/query_market_facts.py --live-fetch`。"
    )

    return "\n".join(lines)


# ── 快照构建主函数 ────────────────────────────────────────────────────────────

def build_snapshot_from_pre_router(
    envelope: dict[str, Any],
    *,
    since_hours: int | None = None,
    major_since_hours: int | None = None,
    use_router: bool | None = None,
    use_rewrite: bool | None = None,
) -> dict[str, Any]:
    """
  将云端 pre-Router JSON（``0.3.0-cloud``）在本地套用 Router/Rewriter 并生成 Markdown。
  ``envelope`` 须含 ``sections`` / ``meta`` / ``errors``（与 FastAPI 响应一致）。
    """
    cloud_meta = dict(envelope.get("meta") or {})
    sections: dict[str, Any] = dict(envelope.get("sections") or {})
    errors: list[dict[str, Any]] = list(envelope.get("errors") or [])

    since_hours = int(since_hours if since_hours is not None else cloud_meta.get("since_hours") or 24)
    major_since_hours = (
        major_since_hours
        if major_since_hours is not None
        else int(cloud_meta.get("major_since_hours") or _default_major_hours())
    )
    use_r = _db_use_router_flag(use_router)
    use_w = _db_use_rewrite_flag(use_rewrite)

    pool_news = sections.get("news") or {}
    deep_sec = sections.get("deep_news") or {}
    sources_ok = list(cloud_meta.get("sources_ok") or [])

    if pool_news:
        news_sec, llm_router_payload, rw_res, _router_res = _apply_router_rewrite_news(
            pool_news,
            deep_items=deep_sec.get("items") or [],
            use_router=use_r,
            use_rewrite=use_w,
            errors=errors,
        )
        sections["news"] = news_sec
        sections["llm_router"] = llm_router_payload
        if rw_res is not None:
            sections["sector_llm_rewrite"] = _rewrite_meta_from_result(rw_res)
            sections["sector_llm_rewrite"]["display"] = {
                sec: {"insight": sr.insight, "items": sr.items, "status": sr.status}
                for sec, sr in rw_res.by_sector.items()
            }
        else:
            sections["sector_llm_rewrite"] = {
                "status": "skipped",
                "display": {},
                "status_by_sector": {},
                "timing_by_sector": {},
                "total_timing_sec": 0.0,
                "timeout_sec": None,
            }
        if news_sec.get("items") and "news" not in sources_ok:
            sources_ok.append("news")

    db_last_at = str(cloud_meta.get("db_last_ingested_at") or cloud_meta.get("fetched_at") or _now_iso())
    tenant = str(cloud_meta.get("tenant_id") or "cloud").strip() or "cloud"
    data_label = f"finance-ingest-cloud (tenant={tenant})"

    md = _build_markdown(
        sections,
        data_label,
        db_last_at,
        since_hours,
        major_since_hours,
    )

    meta_si = dict(cloud_meta.get("social_intelligence") or {})
    if not meta_si and sections.get("social_intelligence"):
        agg = (sections["social_intelligence"].get("aggregate_metrics") or {})
        meta_si = {
            "avg_sentiment": agg.get("avg_sentiment"),
            "headline_sentiment": agg.get("headline_sentiment"),
            "platform_weighted_sentiment": agg.get("platform_weighted_sentiment"),
            "sentiment_label": agg.get("sentiment_label"),
            "fear_greed_index": agg.get("fear_greed_index"),
            "fear_greed_label": agg.get("fear_greed_label"),
            "fear_greed_scope": agg.get("fear_greed_scope"),
            "total_items": agg.get("total_items"),
        }

    meta_base: dict[str, Any] = {
        **cloud_meta,
        "fetched_at": db_last_at,
        "timezone": cloud_meta.get("timezone") or "Asia/Shanghai",
        "sources_ok": sources_ok,
        "data_source": "cloud-mysql+local-router",
        "since_hours": since_hours,
        "major_since_hours": major_since_hours,
        "news_items_count": cloud_meta.get("news_items_count"),
        "db_last_ingested_at": db_last_at,
        "db_use_router": use_r,
        "db_use_rewrite": use_w,
        "cloud_schema_version": envelope.get("schema_version"),
        "llm_router_status": (sections.get("llm_router") or {}).get("status"),
        "llm_router_timing": (sections.get("llm_router") or {}).get("router_timing"),
        "sector_llm_rewrite_status": (sections.get("sector_llm_rewrite") or {}).get("status"),
        "pre_router": False,
    }
    if meta_si:
        meta_base["social_intelligence"] = meta_si

    ok = bool(envelope.get("ok", True))
    if errors and not sections.get("news", {}).get("items"):
        ok = bool(envelope.get("ok", False))

    return {
        "schema_version": "0.3.0-cloud-client",
        "ok": ok,
        "meta": meta_base,
        "sections": sections,
        "errors": errors,
        "markdown_summary": md,
        "invariants": dict(envelope.get("invariants") or {}),
    }


def build_db_snapshot(
    db_path: Path,
    since_hours: int = 24,
    major_since_hours: int | None = None,
    keywords: list[str] | None = None,
    sources: list[str] | None = None,
    *,
    use_router: bool | None = None,
    use_rewrite: bool | None = None,
) -> dict[str, Any]:
    """
    从 DB 读取数据，构建兼容 pipeline.build_snapshot 输出结构的 dict。
    可选：LLM Router、板块润色（仅若干次小请求，较 legacy 全文仍省 token）、离线情绪量化。
    """
    major_since_hours = major_since_hours if major_since_hours is not None else _default_major_hours()
    keywords = keywords or []
    sources = sources or ["market", "news", "social"]
    use_r = _db_use_router_flag(use_router)
    use_w = _db_use_rewrite_flag(use_rewrite)

    conn = _connect(db_path)
    errors: list[dict[str, Any]] = []

    if conn is None:
        return {
            "schema_version": "0.2.2-db",
            "ok": False,
            "meta": {
                "fetched_at": _now_iso(),
                "timezone": "Asia/Shanghai",
                "sources_requested": sources,
                "sources_ok": [],
                "data_source": "db",
                "db_path": str(db_path),
            },
            "sections": {},
            "errors": [{"code": "DB_NOT_FOUND", "message": f"数据库文件不存在：{db_path}，请先运行 ingest.py run 入库。"}],
            "markdown_summary": f"> ⚠️ 数据库不存在（{db_path}）。请先运行 `finance-source-ingest/scripts/ingest.py run` 完成首次入库，或等待定时任务（北京时间 09:00/14:00/20:00）。",
            "invariants": {},
        }

    # 最后入库展示：新闻与市场快照时间的较大值（ISO 字符串可直接 max）
    last_news = conn.execute("SELECT MAX(fetched_at) FROM news_items").fetchone()
    last_mkt = conn.execute("SELECT MAX(snapshot_at) FROM market_snapshots").fetchone()
    ln = str(last_news[0] or "") if last_news and last_news[0] is not None else ""
    lm = str(last_mkt[0] or "") if last_mkt and last_mkt[0] is not None else ""
    parts = [x for x in (ln, lm) if x]
    db_last_at = max(parts) if parts else ""

    news_count = conn.execute(
        "SELECT COUNT(*) FROM news_items WHERE fetched_at >= ?",
        (_cutoff(since_hours),),
    ).fetchone()[0]

    sections: dict[str, Any] = {}
    sources_ok: list[str] = []

    if "market" in sources:
        mkt = _read_market(conn, since_hours)
        sections["market"] = mkt
        if mkt.get("a_share_indices", {}).get("items"):
            sources_ok.append("market")

    if "news" in sources:
        pool_news = _read_news_by_sector(conn, since_hours, keywords)
        deep_sec = _read_deep_news(conn, since_hours)
        sections["deep_news"] = deep_sec
        if deep_sec.get("items"):
            sources_ok.append("deep_news")

        sections["major_events"] = {
            "items": _read_major_event_items(conn, major_since_hours, keywords),
        }

        news_sec, llm_router_payload, rw_res, _router_res = _apply_router_rewrite_news(
            pool_news,
            deep_items=deep_sec.get("items") or [],
            use_router=use_r,
            use_rewrite=use_w,
            errors=errors,
        )
        sections["news"] = news_sec
        sections["llm_router"] = llm_router_payload
        if rw_res is not None:
            sections["sector_llm_rewrite"] = _rewrite_meta_from_result(rw_res)
            sections["sector_llm_rewrite"]["display"] = {
                sec: {"insight": sr.insight, "items": sr.items, "status": sr.status}
                for sec, sr in rw_res.by_sector.items()
            }
        else:
            sections["sector_llm_rewrite"] = {
                "status": "skipped",
                "display": {},
                "status_by_sector": {},
                "timing_by_sector": {},
                "total_timing_sec": 0.0,
                "timeout_sec": None,
            }

        if news_sec.get("items"):
            sources_ok.append("news")

        sections["global_macro"] = {"items": []}
        sections["macro_hot"] = {"items": []}

    if "social" in sources:
        soc = _read_sentiment(conn, since_hours)
        sections["social"] = soc
        if soc.get("items"):
            sources_ok.append("social")

    if {"news", "social"} & set(sources):
        si = _compute_social_intelligence(sections, errors, conn)
        if si:
            sections["social_intelligence"] = si
            _agg = (si.get("aggregate_metrics") or {})
            meta_si = {
                "avg_sentiment": _agg.get("avg_sentiment"),
                "headline_sentiment": _agg.get("headline_sentiment"),
                "platform_weighted_sentiment": _agg.get("platform_weighted_sentiment"),
                "sentiment_label": _agg.get("sentiment_label"),
                "fear_greed_index": _agg.get("fear_greed_index"),
                "fear_greed_label": _agg.get("fear_greed_label"),
                "fear_greed_scope": _agg.get("fear_greed_scope"),
                "total_items": _agg.get("total_items"),
            }
        else:
            meta_si = {}
    else:
        meta_si = {}

    if news_count == 0:
        errors.append({
            "code": "DB_NO_RECENT_DATA",
            "message": f"数据库中最近 {since_hours}h 内无新闻条目。最后入库时间：{db_last_at or '未知'}",
            "hint": "请运行 finance-source-ingest/scripts/ingest.py run 更新数据，或等待定时任务。",
        })

    md = _build_markdown(
        sections,
        str(db_path),
        db_last_at or _now_iso(),
        since_hours,
        major_since_hours,
    )

    meta_base: dict[str, Any] = {
        "fetched_at": db_last_at or _now_iso(),
        "timezone": "Asia/Shanghai",
        "sources_requested": sources,
        "sources_ok": sources_ok,
        "keywords": keywords,
        "data_source": "db",
        "db_path": str(db_path),
        "since_hours": since_hours,
        "major_since_hours": major_since_hours,
        "news_items_count": news_count,
        "db_last_ingested_at": db_last_at,
        "db_use_router": use_r,
        "db_use_rewrite": use_w,
        "llm_router_status": (sections.get("llm_router") or {}).get("status"),
        "llm_router_timing": (sections.get("llm_router") or {}).get("router_timing"),
        "sector_llm_rewrite_status": (sections.get("sector_llm_rewrite") or {}).get("status"),
    }
    if meta_si:
        meta_base["social_intelligence"] = meta_si

    return {
        "schema_version": "0.2.2-db",
        "ok": True,
        "meta": meta_base,
        "sections": sections,
        "errors": errors,
        "markdown_summary": md,
        "invariants": {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="finance-draft-manager DB 快照（不联网）")
    parser.add_argument("--db", default="", help="DB 路径（默认 user_data/finance_sources.db）")
    parser.add_argument("--since-hours", type=int, default=24)
    parser.add_argument("--major-since-hours", type=int, default=None, help="大事件窗口（默认 env FINANCE_DB_SNAPSHOT_MAJOR_HOURS 或 168）")
    parser.add_argument("--no-router", action="store_true", help="禁用 LLM Router（仅规则分桶+补位）")
    parser.add_argument("--no-rewrite", action="store_true", help="禁用板块小润色")
    parser.add_argument("--keywords", default="", help="关键词过滤（空格分隔）")
    parser.add_argument("--sources", default="market,news,social")
    parser.add_argument("--out-dir", default="", help="写 snapshot.json 到该目录（供 preflight 使用）")
    parser.add_argument("--summary-only", action="store_true", help="只输出 meta+markdown_summary+errors")
    parser.add_argument(
        "--pre-router-stdin",
        action="store_true",
        help="从 stdin 读取云端 pre-Router JSON（0.3.0-cloud），本地套用 Router/Rewriter",
    )
    args = parser.parse_args()

    keywords = [k for k in (args.keywords or "").split() if k.strip()]
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    if args.pre_router_stdin:
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps({
                "ok": False,
                "errors": [{"code": "PRE_ROUTER_STDIN_EMPTY", "message": "stdin 为空"}],
            }, ensure_ascii=False, indent=2))
            sys.exit(2)
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(json.dumps({
                "ok": False,
                "errors": [{"code": "PRE_ROUTER_JSON_INVALID", "message": str(exc)[:300]}],
            }, ensure_ascii=False, indent=2))
            sys.exit(2)
        snap = build_snapshot_from_pre_router(
            envelope,
            since_hours=args.since_hours,
            major_since_hours=args.major_since_hours,
            use_router=False if args.no_router else None,
            use_rewrite=False if args.no_rewrite else None,
        )
    else:
        db_path = _resolve_db(args.db)
        snap = build_db_snapshot(
            db_path,
            since_hours=args.since_hours,
            major_since_hours=args.major_since_hours,
            keywords=keywords,
            sources=sources,
            use_router=False if args.no_router else None,
            use_rewrite=False if args.no_rewrite else None,
        )

    # 写 snapshot.json（供 preflight_topic.py 直接读取，兼容旧消费路径）
    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        snap_path = out_dir / "snapshot.json"
        snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    # stdout
    if args.summary_only:
        output = {
            "ok": snap["ok"],
            "schema_version": snap["schema_version"],
            "meta": snap["meta"],
            "errors": snap["errors"],
            "markdown_summary": snap["markdown_summary"],
            "invariants": snap["invariants"],
        }
    else:
        output = snap

    print(json.dumps(output, ensure_ascii=False, indent=2))

    if not snap["ok"] or snap["errors"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
