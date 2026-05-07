"""梯队二：六大板块快讯 — RSSHub（华尔街见闻 / 金十 / 36氪）+ feedparser。

已移除：AkShare 财联社电报、东财 stock_info_global_cls、cls.cn nodeapi 等易触发反爬/超时的路径。
"""

from __future__ import annotations

import html as html_lib
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import BytesIO
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from _common import now_iso

from .sector_keywords import SECTOR_KEYWORDS, SECTOR_ORDER, group_by_sector, tag_news_items

logger = logging.getLogger(__name__)

try:
    import feedparser as _feedparser  # type: ignore[import-untyped]
except ImportError:
    _feedparser = None

RSS_LEGACY_SNIPPET = r"""
# 旧版：AkShare 财联社 + cls.cn nodeapi（已移除，见 git 历史）。
"""

# （路径, 路由标识）— 用于 meta / 日志
RSS_NEWS_ROUTES: tuple[tuple[str, str], ...] = (
    ("/wallstreetcn/live", "wsc_live"),
    ("/jin10", "jin10"),
    ("/36kr/newsflashes", "kr36"),
)

RSS_NEWS_FILTER_KEYWORDS: tuple[str, ...] = (
    "科技",
    "新能源",
    "黄金",
    "港股",
    "有色",
    "银行",
    "降息",
    "美联储",
    "央行",
    "CPI",
)

_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)

_FETCH_TIMEOUT_SEC = 10.0
_MAX_ENTRIES_PER_ROUTE = 40
_CLS_POOL_LIMIT = 160


def _html_to_plain(text: str) -> str:
    if not text:
        return ""
    t = _TAG_RE.sub(" ", text)
    t = html_lib.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _normalize_rsshub_base(raw: str | None) -> str | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip().strip('"').strip("'")
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    s = s.rstrip("/")
    if not s.startswith(("http://", "https://")):
        return None
    return s


def _rsshub_base_from_env() -> str | None:
    return _normalize_rsshub_base(os.environ.get("FINANCE_RSSHUB_BASE_URL"))


def _entry_sort_ts(entry: Any) -> float:
    for key in ("published_parsed", "updated_parsed"):
        t = None
        if hasattr(entry, "get"):
            t = entry.get(key)
        if t is None:
            t = getattr(entry, key, None)
        if t:
            try:
                return time.mktime(t)
            except Exception:  # noqa: BLE001
                continue
    return 0.0


def _format_published_from_entry(entry: Any) -> str:
    """统一为 YYYY-MM-DD HH:MM:SS（优先结构化时间，其次 RSS 原文）。"""
    ts = None
    if hasattr(entry, "get"):
        ts = entry.get("published_parsed") or entry.get("updated_parsed")
    if ts:
        try:
            dt = datetime.fromtimestamp(time.mktime(ts))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            pass
    pub = ""
    if hasattr(entry, "get"):
        pub = str(entry.get("published") or entry.get("updated") or "").strip()
    if not pub:
        return ""
    s19 = pub[:19]
    if "T" in s19:
        s19 = s19.replace("T", " ", 1)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s19[: len(fmt)], fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return pub[:19]


def _entry_passes_keyword_filter(title: str, clean_text: str) -> bool:
    blob = f"{title}{clean_text}"
    low = blob.lower()
    for kw in RSS_NEWS_FILTER_KEYWORDS:
        if kw == "CPI":
            if "cpi" in low:
                return True
        elif kw in blob:
            return True
    return False


def _fetch_one_route(base: str, path: str, route_key: str) -> list[dict[str, Any]]:
    if _feedparser is None:
        raise RuntimeError("feedparser 未安装")

    url = f"{base}{path}"
    req = urlrequest.Request(url, headers={"User-Agent": "finance-source-ingest/0.2 (news_rsshub)"})
    with urlrequest.urlopen(req, timeout=_FETCH_TIMEOUT_SEC) as resp:
        body = resp.read()

    parsed = _feedparser.parse(BytesIO(body))
    if getattr(parsed, "bozo", False) and not getattr(parsed, "entries", None):
        exc = getattr(parsed, "bozo_exception", None)
        raise ValueError(f"feed 解析失败: {exc}")

    entries = list(getattr(parsed, "entries", []) or [])
    entries.sort(key=_entry_sort_ts, reverse=True)

    out: list[dict[str, Any]] = []
    for ent in entries[:_MAX_ENTRIES_PER_ROUTE]:
        title = ""
        if hasattr(ent, "get"):
            title = str(ent.get("title") or "").strip()
        summary = ""
        if hasattr(ent, "get"):
            summary = ent.get("summary") or ent.get("description") or ""
        summary = str(summary or "")

        plain_full = _html_to_plain(summary).strip()
        body_line = plain_full if plain_full else title
        clean_text = body_line[:200] if len(body_line) > 200 else body_line
        if not _entry_passes_keyword_filter(title, clean_text):
            continue

        published_at = _format_published_from_entry(ent)
        if not title and not clean_text:
            continue
        out.append(
            {
                "title": title or clean_text[:120],
                "clean_text": clean_text,
                "published_at": published_at,
            }
        )
    return out


def _safe_fetch_route(base: str, path: str, route_key: str) -> tuple[list[dict[str, Any]], bool]:
    try:
        return _fetch_one_route(base, path, route_key), True
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
        logger.warning("news_rsshub route %s failed: %s", path, exc)
        return [], False
    except Exception as exc:  # noqa: BLE001
        logger.warning("news_rsshub route %s failed: %s", path, exc)
        return [], False


def _fetch_cls_via_akshare(errors: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    try:
        import akshare as ak  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        errors.append(
            {
                "source": "news",
                "stage": "cls_akshare_import",
                "code": "CLS_AKSHARE_IMPORT_FAILED",
                "message": repr(e),
                "hint": "pip install akshare pandas",
            }
        )
        return [], False

    frames: list[Any] = []
    ok = False
    for sym in ("重点", "全部"):
        try:
            df = ak.stock_info_global_cls(symbol=sym)
            if df is not None and not getattr(df, "empty", True):
                frames.append(df)
                ok = True
        except Exception as e:  # noqa: BLE001
            logger.warning("news cls akshare %s failed: %s", sym, e)
    if not frames:
        return [], ok
    work = pd.concat(frames, ignore_index=True)
    title_col = "标题" if "标题" in work.columns else None
    body_col = "内容" if "内容" in work.columns else None
    if not title_col or not body_col:
        return [], ok
    out: list[dict[str, Any]] = []
    for _, row in work.tail(_CLS_POOL_LIMIT).iterrows():
        title = str(row.get(title_col) or "").strip()
        body = str(row.get(body_col) or "").strip()
        clean = body[:200] if len(body) > 200 else body
        if not title and not clean:
            continue
        if not _entry_passes_keyword_filter(title, clean):
            continue
        pub = ""
        # stock_info_global_cls 常见是 "发布时间" + "发布日期"
        if "发布时间" in work.columns:
            pub = str(row.get("发布时间") or "").strip()
        if "发布日期" in work.columns:
            d = str(row.get("发布日期") or "").strip()
            if d and pub and len(pub) <= 8:
                pub = f"{d} {pub}"
            elif d and not pub:
                pub = d
        out.append(
            {
                "title": title or clean[:120],
                "clean_text": clean or title,
                "published_at": pub,
                "source_hint": "cls_akshare",
            }
        )
    return out, ok


def _normalize_title_key(s: str) -> str:
    t = re.sub(r"\s+", "", (s or "").lower())
    return t[:30]


def _merge_news_sources(
    rss_items: list[dict[str, Any]],
    cls_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cls_keys = {_normalize_title_key(str(x.get("title") or "")) for x in cls_items}
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for it in [*rss_items, *cls_items]:
        k = (str(it.get("title") or "")[:160], str(it.get("published_at") or ""))
        if k in seen:
            continue
        seen.add(k)
        neo = dict(it)
        tkey = _normalize_title_key(str(neo.get("title") or ""))
        neo["cross_source_hit"] = tkey in cls_keys and any(_normalize_title_key(str(r.get("title") or "")) == tkey for r in rss_items)
        merged.append(neo)
    merged.sort(
        key=lambda x: (
            1 if x.get("cross_source_hit") else 0,
            _parse_published_ts(x.get("published_at")) or datetime.min,
        ),
        reverse=True,
    )
    return merged


def fetch_news_via_rsshub(base: str) -> tuple[list[dict[str, Any]], list[str]]:
    """并发拉取多路由；单路由异常不冒泡。返回 (合并去重后的条目, 成功完成解析的路由 key 列表)。"""
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    routes_ok: list[str] = []

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {
            pool.submit(_safe_fetch_route, base, path, key): (path, key)
            for path, key in RSS_NEWS_ROUTES
        }
        for fut in as_completed(futs):
            path, key = futs[fut]
            items, ok = fut.result()
            if ok:
                routes_ok.append(key)
            for it in items:
                dedup_k = (str(it.get("title") or "")[:160], str(it.get("published_at") or ""))
                if dedup_k in seen:
                    continue
                seen.add(dedup_k)
                merged.append(it)

    merged.sort(
        key=lambda x: _parse_published_ts(x.get("published_at")) or datetime.min,
        reverse=True,
    )
    return merged, routes_ok


def news_hhmm_for_markdown(published_at: str) -> str:
    if not published_at or len(published_at) < 16:
        return "--:--"
    try:
        return published_at[11:16]
    except Exception:  # noqa: BLE001
        return "--:--"


def _keyword_or_match(item: dict[str, Any], keywords: list[str]) -> bool:
    blob = f"{item.get('title') or ''} {item.get('clean_text') or ''}".lower()
    return any(k.strip().lower() in blob for k in keywords if k.strip())


def _parse_published_ts(val: Any) -> datetime | None:
    if val is None:
        return None
    s = str(val).strip()
    if len(s) < 10:
        return None
    s19 = s[:19] if len(s) >= 19 else s
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s19, fmt)
        except ValueError:
            continue
    return None


def _enrich_sector_buckets(
    all_tagged: list[dict[str, Any]],
    *,
    max_per_sector: int,
    min_fill: int,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    buckets: dict[str, list[dict[str, Any]]] = {s: [] for s in SECTOR_ORDER}

    def item_key(it: dict[str, Any]) -> tuple[str, str]:
        return (str(it.get("title") or ""), str(it.get("published_at") or ""))

    sorted_items = sorted(
        [x for x in all_tagged if isinstance(x, dict)],
        key=lambda x: _parse_published_ts(x.get("published_at")) or datetime.min,
        reverse=True,
    )

    def dedup_append(sec: str, it: dict[str, Any], source: str) -> None:
        cur = buckets[sec]
        if len(cur) >= max_per_sector:
            return
        k = item_key(it)
        if any(item_key(x) == k for x in cur):
            return
        neo = dict(it)
        neo["sector_line_source"] = source
        cur.append(neo)

    for it in sorted_items:
        for sec in it.get("sector_tags") or []:
            if sec in buckets:
                dedup_append(sec, it, "tagged")

    for sec in SECTOR_ORDER:
        if len(buckets[sec]) >= min_fill:
            continue
        kws = SECTOR_KEYWORDS.get(sec, ())
        for it in sorted_items:
            if len(buckets[sec]) >= max_per_sector:
                break
            blob = f"{it.get('title') or ''}{it.get('clean_text') or ''}"
            if not any(k and (k in blob) for k in kws):
                continue
            if any(item_key(it) == item_key(x) for x in buckets[sec]):
                continue
            tags = it.get("sector_tags") or []
            src = "recent_keyword" if sec not in tags else "tagged_catchup"
            dedup_append(sec, it, src)

    relax_used = any(
        x.get("sector_line_source") in ("recent_keyword", "tagged_catchup")
        for lst in buckets.values()
        for x in lst
    )
    return buckets, relax_used


def _build_items_from_raw_list(
    raw_items: list[dict[str, Any]],
    keywords: list[str],
    cap: int,
    errors: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    bool,
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    bool,
    list[dict[str, Any]],
    bool,
]:
    keyword_fallback = False
    kws = [k for k in (keywords or []) if str(k).strip()]
    if kws:
        filtered = [x for x in raw_items if _keyword_or_match(x, kws)]
        if filtered:
            working = filtered
        else:
            working = raw_items[: max(cap, 12)]
            keyword_fallback = True
            errors.append(
                {
                    "source": "news",
                    "stage": "keywords",
                    "code": "NEWS_KEYWORD_FALLBACK",
                    "message": "关键词 OR 过滤无命中，已退回 RSSHub 原文再打板块标签",
                    "hint": str(kws[:12]),
                }
            )
    else:
        working = raw_items

    full_tagged = tag_news_items(raw_items)
    tagged = tag_news_items(working)
    sector_hits = [x for x in tagged if x.get("sector_tags")]
    sector_filter_fallback = False

    if sector_hits:
        out = sector_hits[:cap]
    else:
        out = tagged[: min(5, cap)]
        sector_filter_fallback = True
        errors.append(
            {
                "source": "news",
                "stage": "sector_filter",
                "code": "SECTOR_FILTER_EMPTY",
                "message": "本轮快讯未命中六大板块关键词，已降级展示最新若干条原文",
                "hint": "科技/新能源/港股/黄金/有色/银行",
            }
        )

    items_by_sector, sector_relax_used = _enrich_sector_buckets(
        full_tagged,
        max_per_sector=5,
        min_fill=3,
    )
    _, unclassified = group_by_sector(out)
    other_flash = [x for x in full_tagged if isinstance(x, dict) and not (x.get("sector_tags") or [])][:28]

    return (
        out[:cap],
        keyword_fallback,
        items_by_sector,
        unclassified,
        sector_filter_fallback,
        other_flash,
        sector_relax_used,
    )


def _news_cap(max_items: int) -> int:
    return max(1, min(int(max_items), 48))


def fetch_news_section(
    keywords: list[str],
    max_items: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    cap = _news_cap(max_items)

    base = _rsshub_base_from_env()
    if not base:
        errors.append(
            {
                "source": "news",
                "stage": "rsshub",
                "code": "NEWS_RSSHUB_BASE_URL_MISSING",
                "message": "未设置或无效的 FINANCE_RSSHUB_BASE_URL（需 http(s):// 主机，无尾斜杠）",
                "hint": "示例: FINANCE_RSSHUB_BASE_URL=http://127.0.0.1:1200",
            }
        )
        return _empty_news_section("rsshub:unconfigured"), errors

    if _feedparser is None:
        errors.append(
            {
                "source": "news",
                "stage": "import",
                "code": "NEWS_FEEDPARSER_IMPORT_ERROR",
                "message": "Module feedparser not installed",
                "hint": "pip install feedparser",
            }
        )
        return _empty_news_section("rsshub:feedparser_missing"), errors

    rss_items, routes_ok = fetch_news_via_rsshub(base)
    cls_items, cls_ok = _fetch_cls_via_akshare(errors)
    merged = _merge_news_sources(rss_items, cls_items)

    if not routes_ok and not cls_ok:
        errors.append(
            {
                "source": "news",
                "stage": "rsshub",
                "code": "NEWS_RSSHUB_ROUTES_FAILED",
                "message": "RSSHub 与 AkShare 财联社双源均失败",
                "hint": "+".join(p for p, _ in RSS_NEWS_ROUTES),
            }
        )
    elif not merged:
        errors.append(
            {
                "source": "news",
                "stage": "rsshub",
                "code": "NEWS_RSSHUB_FILTER_EMPTY",
                "message": "双源已返回但标题/正文均未命中关键词过滤",
                "hint": ",".join(RSS_NEWS_FILTER_KEYWORDS),
            }
        )

    if not merged:
        return (
            {
                "as_of": now_iso(),
                "items": [],
                "sources_used": [f"RSSHub:{'+'.join(routes_ok) or 'none'}"],
                "source_primary": f"rsshub:{'+'.join(routes_ok) or 'none'}",
                "keyword_fallback": False,
                "items_by_sector": {s: [] for s in SECTOR_ORDER},
                "sector_filter_fallback": False,
                "items_unclassified": [],
                "items_other_flash": [],
                "sector_relax_backfill": False,
                "rsshub_paths_ok": routes_ok,
                "cls_source_ok": cls_ok,
            },
            errors,
        )

    (
        items,
        keyword_fallback,
        items_by_sector,
        items_unclassified,
        sector_filter_fallback,
        items_other_flash,
        sector_relax_used,
    ) = _build_items_from_raw_list(merged, keywords, cap, errors)

    return {
        "as_of": now_iso(),
        "items": items,
        "sources_used": [f"RSSHub:{'+'.join(routes_ok)}"],
        "source_primary": f"rsshub+cls:{'+'.join(routes_ok) or 'none'}",
        "keyword_fallback": keyword_fallback,
        "items_by_sector": items_by_sector,
        "sector_filter_fallback": sector_filter_fallback,
        "items_unclassified": items_unclassified,
        "items_other_flash": items_other_flash,
        "sector_relax_backfill": sector_relax_used,
        "rsshub_paths_ok": routes_ok,
        "cls_source_ok": cls_ok,
    }, errors


def _empty_news_section(source_primary: str) -> dict[str, Any]:
    return {
        "as_of": now_iso(),
        "items": [],
        "sources_used": [],
        "source_primary": source_primary,
        "keyword_fallback": False,
        "items_by_sector": {s: [] for s in SECTOR_ORDER},
        "sector_filter_fallback": False,
        "items_unclassified": [],
        "items_other_flash": [],
        "sector_relax_backfill": False,
        "rsshub_paths_ok": [],
    }
