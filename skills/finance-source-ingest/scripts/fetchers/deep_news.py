"""深度内容层：华尔街见闻（快讯+文章 API）/ 第一财经 / 界面新闻 / 金十数据 RSS（独立容灾）。

每条 item 附规则 based 情感标注（sentiment_hint / impact_level / stock_mentions）。
各源独立 try/except，任一失败不影响其余，最终统一输出 section_data + errors。

环境变量：
  FINANCE_RSSHUB_BASE_URL — 自建 RSSHub 根 URL（无尾斜杠），如 http://127.0.0.1:1200 ；
    设置后优先从 RSSHub 拉取华尔街见闻/金十/第一财经/界面/36氪，再回退直连 API 与官方 RSS。
付费 API 接入口（预留）：
  FINANCE_DEEP_NEWS_PROVIDER=<provider_key>
  当前版本识别后写入日志并降级到 RSS/HTTP，实装时在 _PROVIDER_DISPATCH 注册对应函数。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import request as urlrequest

from .sector_keywords import SECTOR_ORDER, sectors_for_text
from .sentiment import classify_impact, classify_sentiment, extract_stock_mentions, sentiment_emoji

logger = logging.getLogger(__name__)

# ——— 深度内容源配置 ——————————————————————————————————————————————————————

_SUMMARY_MAX_LEN = 120  # 约 2-3 句核心信息，与选题 digest 上限对齐
_ARTICLE_TEXT_MAX_LEN = 1000  # 文章源 clean_text 供 LLM 消费
_FRESH_DAYS_DEFAULT = 7

# 直连 API / RSS（无 RSSHub 或作回退）
_BASE_DEEP_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "key": "wallstreetcn",
        "name": "华尔街见闻",
        "type": "api_json",
        # 先快讯 lives，再降级 articles
        "urls": (
            "https://api.wallstreetcn.com/apiv1/content/lives?channel=alldoc&cursor=0&num=20",
            "https://api.wallstreetcn.com/apiv1/content/articles?channel=1&cursor=0&num=20",
        ),
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; finance-source-ingest/0.3)",
            "Accept": "application/json",
            "Referer": "https://www.wallstreetcn.com/",
        },
        "timeout": 15,
    },
    {
        "key": "yicai",
        "name": "第一财经",
        "type": "rss",
        "url_candidates": (
            "https://www.yicai.com/rss/news/",
            "https://www.yicai.com/rss/list/",
            "https://www.yicai.com/rss/",
        ),
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; finance-source-ingest/0.3)",
        },
        "timeout": 15,
    },
    {
        "key": "jin10",
        "name": "金十数据",
        "type": "rss",
        "url": "https://rss.jin10.com/",
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; finance-source-ingest/0.3)",
        },
        "timeout": 15,
    },
    {
        "key": "jiemian",
        "name": "界面新闻",
        "type": "rss",
        "url": "https://www.jiemian.com/rss/",
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; finance-source-ingest/0.3)",
        },
        "timeout": 15,
    },
)

# 付费接入 dispatch 表（key → callable）；实装时在此注册
_PROVIDER_DISPATCH: dict[str, Any] = {}

_RSSHUB_ROUTE_ROWS: tuple[tuple[str, str, str], ...] = (
    ("wallstreetcn_hot_rsshub", "华尔街见闻最热(RSSHub)", "/wallstreetcn/hot"),
    ("cls_depth_rsshub", "财联社深度(RSSHub)", "/cls/depth"),
    ("yicai_feed_rsshub", "第一财经深度(RSSHub)", "/yicai/feed"),
    ("gelonghui_home_rsshub", "格隆汇首页(RSSHub)", "/gelonghui/home"),
    ("kr36_tech_info_rsshub", "36氪科技深度(RSSHub)", "/36kr/information/technology"),
    ("xueqiu_hots_rsshub", "雪球热帖(RSSHub)", "/xueqiu/hots"),
)

# v0.1.9：六大板块垂直 RSSHub 矩阵（仅当配置了 FINANCE_RSSHUB_BASE_URL 时启用，替代扁平 _RSSHUB_ROUTE_ROWS）。
# needs_probe：路由依赖上游站点/RSSHub 版本，部署后需在 sector_rsshub_matrix 中看 routes_failed 验收。
SECTOR_DEEP_RSSHUB_ROUTES: dict[str, dict[str, list[dict[str, Any]]]] = {
    "科技": {
        "primary": [
            {
                "path": "/36kr/information/technology",
                "label": "36氪科技前沿文章",
                "key": "vertical_tech_kr36_info_technology",
                "needs_probe": True,
            },
            {
                "path": "/huxiu/channel/103",
                "label": "虎嗅科技频道",
                "key": "vertical_tech_huxiu_channel_103",
                "needs_probe": True,
            },
        ],
        "fallback": [
            {
                "path": "/xueqiu/hots",
                "label": "雪球热帖（科技宽池）",
                "key": "vertical_tech_xueqiu_hots_fb",
                "needs_probe": True,
            },
        ],
    },
    "新能源": {
        "primary": [
            {
                "path": "/jiemian/lists/84",
                "label": "界面汽车/新能源",
                "key": "vertical_ev_jiemian_84",
                "needs_probe": True,
            },
        ],
        "fallback": [
            {
                "path": "/xueqiu/hots",
                "label": "雪球热帖（新能源宽池）",
                "key": "vertical_ev_xueqiu_hots_fb",
                "needs_probe": True,
            },
            {
                "path": "/wallstreetcn/hot",
                "label": "华尔街见闻最热（宽池）",
                "key": "vertical_ev_wscn_hot_fb",
                "needs_probe": True,
            },
        ],
    },
    "港股": {
        "primary": [
            {
                "path": "/gelonghui/home",
                "label": "格隆汇首页深度",
                "key": "vertical_hk_gelonghui_home",
                "needs_probe": True,
            },
        ],
        "fallback": [
            {
                "path": "/xueqiu/hots",
                "label": "雪球热帖（港股宽池）",
                "key": "vertical_hk_xueqiu_hots_fb",
                "needs_probe": True,
            },
        ],
    },
    "黄金": {
        "primary": [
            {
                "path": "/wallstreetcn/hot",
                "label": "华尔街见闻最热（贵金属/宏观）",
                "key": "vertical_gold_wscn_hot",
                "needs_probe": True,
            },
            {
                "path": "/cls/depth",
                "label": "财联社深度（贵金属/宏观）",
                "key": "vertical_gold_cls_depth",
                "needs_probe": True,
            },
        ],
        "fallback": [
            {
                "path": "/xueqiu/hots",
                "label": "雪球热帖（黄金宽池）",
                "key": "vertical_gold_xueqiu_hots_fb",
                "needs_probe": True,
            },
        ],
    },
    "有色": {
        "primary": [
            {
                "path": "/wallstreetcn/hot",
                "label": "华尔街见闻最热（工业金属/宏观）",
                "key": "vertical_metals_wscn_hot",
                "needs_probe": True,
            },
            {
                "path": "/cls/depth",
                "label": "财联社深度（工业金属/宏观）",
                "key": "vertical_metals_cls_depth",
                "needs_probe": True,
            },
        ],
        "fallback": [
            {
                "path": "/xueqiu/hots",
                "label": "雪球热帖（有色宽池）",
                "key": "vertical_metals_xueqiu_hots_fb",
                "needs_probe": True,
            },
        ],
    },
    "银行": {
        "primary": [
            {
                "path": "/yicai/feed",
                "label": "第一财经深度 feed",
                "key": "vertical_bank_yicai_feed",
                "needs_probe": False,
            },
            {
                "path": "/cls/depth",
                "label": "财联社深度（大金融）",
                "key": "vertical_bank_cls_depth",
                "needs_probe": True,
            },
        ],
        "fallback": [
            {
                "path": "/xueqiu/hots",
                "label": "雪球热帖（大金融宽池）",
                "key": "vertical_bank_xueqiu_hots_fb",
                "needs_probe": True,
            },
        ],
    },
}


def _rsshub_base() -> str:
    return os.environ.get("FINANCE_RSSHUB_BASE_URL", "").strip().rstrip("/")


def _rsshub_layer_sources(base: str) -> list[dict[str, Any]]:
    """RSSHub 前置源（与官方路由一致；需自建实例可访问上游站）。"""
    hdr = {
        "User-Agent": "Mozilla/5.0 (compatible; finance-source-ingest/0.4 RSSHub)",
        "Accept": "application/rss+xml, application/atom+xml, */*",
    }
    timeout = 25
    out: list[dict[str, Any]] = []
    for key, name, path in _RSSHUB_ROUTE_ROWS:
        p = path if path.startswith("/") else f"/{path}"
        out.append({
            "key": key,
            "name": name,
            "type": "rss",
            "url": f"{base}{p}",
            "headers": hdr,
            "timeout": timeout,
        })
    return out


def merged_deep_source_list() -> list[dict[str, Any]]:
    """RSSHub 层（若配置了 FINANCE_RSSHUB_BASE_URL）+ 直连源顺序。"""
    base = _rsshub_base()
    if base:
        return _rsshub_layer_sources(base) + list(_BASE_DEEP_SOURCES)
    return list(_BASE_DEEP_SOURCES)


def _title_dedup_key(title: str) -> str:
    t = re.sub(r"\s+", "", (title or "").strip().lower())
    return t[:120] if t else ""


def _fetch_sector_vertical_rsshub(
    base: str,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """按六大板块遍历 RSSHub 路由；失败原因写入 errors，汇总进 matrix。"""
    per_route_limit = max(8, min(24, max(4, limit) * 2))
    hdr = {
        "User-Agent": "Mozilla/5.0 (compatible; finance-source-ingest/0.5 sector-rsshub)",
        "Accept": "application/rss+xml, application/atom+xml, */*",
    }
    timeout = 25
    all_items: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    by_sector: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []

    for sector in SECTOR_ORDER:
        cfg = SECTOR_DEEP_RSSHUB_ROUTES.get(sector, {})
        primary = list(cfg.get("primary") or [])
        fallback = list(cfg.get("fallback") or [])
        tried_detail: list[dict[str, Any]] = []
        routes_ok: list[str] = []
        routes_failed: list[dict[str, Any]] = []
        sector_bucket: list[dict[str, Any]] = []

        def run_phase(routes: list[dict[str, Any]], phase: str) -> None:
            for spec in routes:
                path = str(spec.get("path") or "").strip()
                if not path.startswith("/"):
                    path = "/" + path
                key = str(spec.get("key") or path)
                label = str(spec.get("label") or key)
                needs_probe = bool(spec.get("needs_probe"))
                tried_detail.append({
                    "phase": phase,
                    "path": path,
                    "key": key,
                    "label": label,
                    "needs_probe": needs_probe,
                })
                src = {
                    "key": key,
                    "name": label,
                    "type": "rss",
                    "url": f"{base}{path}",
                    "headers": hdr,
                    "timeout": timeout,
                }
                items, errs = _fetch_rss(src, per_route_limit)
                for e in errs:
                    errors.append({
                        "source": "deep_news",
                        "stage": "sector_vertical_rsshub",
                        "sector": sector,
                        "path": path,
                        "deep_route_key": key,
                        "code": str(e.get("code") or "SECTOR_RSSHUB_ROUTE_FAILED"),
                        "message": str(e.get("message") or "")[:400],
                    })
                if items:
                    routes_ok.append(key)
                    for raw in items:
                        tit = str(raw.get("title") or "")
                        url_s = str(raw.get("url") or "").strip()
                        dk = _title_dedup_key(tit)
                        dedup_token = dk if dk else (url_s[:200] if url_s else tit[:80])
                        pair = (sector, dedup_token)
                        if pair in seen_pairs:
                            continue
                        seen_pairs.add(pair)
                        neo = dict(raw)
                        neo["vertical_target_sector"] = sector
                        neo["deep_route_key"] = key
                        sector_bucket.append(neo)
                else:
                    last = errs[-1] if errs else {}
                    routes_failed.append({
                        "phase": phase,
                        "path": path,
                        "key": key,
                        "code": str(last.get("code") or "SECTOR_RSSHUB_EMPTY"),
                        "message": str(last.get("message") or "EMPTY_FEED")[:300],
                    })

        run_phase(primary, "primary")
        if not sector_bucket:
            run_phase(fallback, "fallback")

        all_items.extend(sector_bucket)
        by_sector[sector] = {
            "routes_tried_detail": tried_detail,
            "routes_ok": routes_ok,
            "routes_failed": routes_failed,
            "items_count": len(sector_bucket),
        }

    matrix: dict[str, Any] = {
        "by_sector": by_sector,
        "rsshub_base_url": base,
    }
    return all_items, matrix, errors


# ——— 文本工具 ——————————————————————————————————————————————————————————

def _strip_html(text: str) -> str:
    """去除 HTML 标签，折叠连续空白。"""
    clean = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", clean).strip()


def _clip_summary(text: str, max_len: int = _SUMMARY_MAX_LEN) -> str:
    """截取到 max_len 字，尽量在中文句号/问号/感叹号处断行。"""
    t = _strip_html(text).strip()
    if not t:
        return ""
    if len(t) <= max_len:
        return t
    for punct in ("。", "！", "？", ".", "!", "?"):
        pos = t.rfind(punct, 0, max_len)
        if pos > max_len // 2:
            return t[: pos + 1]
    return t[:max_len] + "…"


def _clip_article_text(text: str, max_len: int = _ARTICLE_TEXT_MAX_LEN) -> str:
    """文章正文清洗与截断（默认 1000 字，供下游 LLM 消费）。"""
    t = _strip_html(text).strip()
    if not t:
        return ""
    if len(t) <= max_len:
        return t
    return t[:max_len] + "…"


def _parse_time(raw: Any) -> str:
    """把各种时间格式归一化为 ISO8601 字符串（Asia/Shanghai）。"""
    tz_cn = timezone(timedelta(hours=8))
    raw_s = str(raw or "").strip()
    if not raw_s:
        return datetime.now(tz_cn).isoformat(timespec="seconds")
    # Unix timestamp
    try:
        ts = int(float(raw_s))
        if 1_000_000_000 < ts < 9_999_999_999:
            return datetime.fromtimestamp(ts, tz=tz_cn).isoformat(timespec="seconds")
    except (ValueError, TypeError, OSError):
        pass
    # ISO / RFC variants
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
    ):
        try:
            trimmed = raw_s[: len(fmt) + 6]
            dt = datetime.strptime(trimmed, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz_cn)
            return dt.astimezone(tz_cn).isoformat(timespec="seconds")
        except (ValueError, TypeError):
            continue
    return raw_s[:19] if len(raw_s) >= 10 else raw_s


def _parse_iso_dt(raw: str) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt


def _fresh_days_limit() -> int:
    raw = os.environ.get("FINANCE_DEEP_NEWS_FRESH_DAYS", "").strip()
    try:
        v = int(raw) if raw else _FRESH_DAYS_DEFAULT
    except ValueError:
        v = _FRESH_DAYS_DEFAULT
    return max(1, min(30, v))


def _filter_recent_items(items: list[dict[str, Any]], *, days: int) -> tuple[list[dict[str, Any]], int]:
    """仅保留最近 N 天深度条目；无法解析时间的条目默认保留。"""
    now_dt = datetime.now(timezone(timedelta(hours=8)))
    floor = now_dt - timedelta(days=days)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        dt = _parse_iso_dt(str(it.get("published_at") or ""))
        if dt is not None and dt < floor:
            dropped += 1
            continue
        kept.append(it)
    return kept, dropped


def _fetch_url(url: str, headers: dict[str, str], timeout: int = 15) -> bytes:
    req = urlrequest.Request(url, headers=headers)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ——— 情感 + 板块标注 ——————————————————————————————————————————————————————

def _enrich(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量为 deep_news items 打情感/影响/板块/股票标签。"""
    out: list[dict[str, Any]] = []
    for it in items:
        text = f"{it.get('title') or ''} {it.get('summary') or ''}"
        s = classify_sentiment(text)
        enriched = dict(it)
        enriched["sentiment_hint"] = s
        enriched["sentiment_emoji"] = sentiment_emoji(s)
        enriched["impact_level"] = classify_impact(text)
        enriched["stock_mentions"] = extract_stock_mentions(text)
        tags = list(sectors_for_text(text))
        vts = enriched.get("vertical_target_sector")
        if isinstance(vts, str) and vts in SECTOR_ORDER and vts not in tags:
            tags.insert(0, vts)
        enriched["sector_tags"] = tags
        out.append(enriched)
    return out


# ——— 华尔街见闻 JSON API（多 URL 链式） —————————————————————————————————

def _wscn_raw_item_list(data: dict[str, Any]) -> list[Any]:
    """兼容 lives / articles 等嵌套结构。"""
    d = data.get("data")
    if not isinstance(d, dict):
        return []
    for key in ("items", "lives", "lines", "records", "children"):
        lst = d.get(key)
        if isinstance(lst, list) and lst:
            return lst
    return []


def _wscn_parse_one_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    title = str(
        entry.get("title")
        or entry.get("content_title")
        or entry.get("name")
        or "",
    ).strip()
    body = (
        entry.get("summary")
        or entry.get("content_text")
        or entry.get("content")
        or entry.get("text")
        or entry.get("description")
        or ""
    )
    if not title and isinstance(body, str) and body.strip():
        title = _clip_summary(body, max_len=80)
    if not title:
        return None
    clean_text = _clip_article_text(str(body) if body else title)
    summary = _clip_summary(clean_text or title)
    uri = str(entry.get("uri") or entry.get("resource_uri") or entry.get("url") or "").strip()
    url_full = ""
    if uri.startswith("http://") or uri.startswith("https://"):
        url_full = uri
    elif uri.startswith("/"):
        url_full = f"https://www.wallstreetcn.com{uri}"
    elif entry.get("id") is not None:
        wid = str(entry.get("id")).strip()
        if wid.isdigit():
            url_full = f"https://wallstreetcn.com/livenews/{wid}"
    ts = (
        entry.get("display_time")
        or entry.get("created_at")
        or entry.get("display_time_integer")
        or entry.get("created_at_timestamp")
        or entry.get("time")
        or ""
    )
    return {
        "title": title,
        "summary": summary,
        "clean_text": clean_text,
        "url": url_full,
        "published_at": _parse_time(ts),
    }


def _fetch_wallstreetcn(src: dict[str, Any], limit: int) -> tuple[list[dict], list[dict]]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    urls: tuple[str, ...] | list[str]
    if src.get("urls"):
        urls = tuple(src["urls"])
    elif src.get("url"):
        urls = (str(src["url"]),)
    else:
        errors.append({"code": "WALLSTREETCN_API_FAILED", "message": "无 URL 配置"})
        return items, errors

    for api_url in urls:
        try:
            raw = _fetch_url(api_url, src["headers"], src.get("timeout", 15))
            data = json.loads(raw.decode("utf-8", errors="ignore"))
            raw_items = _wscn_raw_item_list(data if isinstance(data, dict) else {})
            batch: list[dict[str, Any]] = []
            for entry in raw_items[:limit]:
                if not isinstance(entry, dict):
                    continue
                parsed = _wscn_parse_one_entry(entry)
                if not parsed:
                    continue
                parsed["source"] = src["key"]
                parsed["source_name"] = src["name"]
                batch.append(parsed)
            if batch:
                return batch, errors
            errors.append({
                "code": "WALLSTREETCN_API_EMPTY",
                "message": f"URL 返回空条: {api_url[:120]}",
            })
        except Exception as exc:  # noqa: BLE001
            errors.append({
                "code": "WALLSTREETCN_API_FAILED",
                "message": str(exc)[:300],
            })
            logger.warning("deep_news wallstreetcn try %s failed: %s", api_url, exc)
    return items, errors


# ——— RSS 通用拉取（第一财经 / 界面新闻） ————————————————————————————————

def _fetch_rss(src: dict[str, Any], limit: int) -> tuple[list[dict], list[dict]]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        import feedparser  # noqa: PLC0415  # already in requirements.txt

        raw = _fetch_url(src["url"], src["headers"], src.get("timeout", 15))
        feed = feedparser.parse(raw)
        for entry in (feed.entries or [])[:limit]:
            title = str(getattr(entry, "title", "") or "").strip()
            if not title:
                continue
            raw_summary = (
                getattr(entry, "summary", "")
                or getattr(entry, "description", "")
                or title
            )
            clean_text = _clip_article_text(raw_summary)
            summary = _clip_summary(clean_text or title)
            link = str(getattr(entry, "link", "") or "").strip()
            pub_raw = (
                getattr(entry, "published", "")
                or getattr(entry, "updated", "")
                or ""
            )
            items.append({
                "title": title,
                "summary": summary,
                "clean_text": clean_text,
                "url": link,
                "published_at": _parse_time(pub_raw),
                "source": src["key"],
                "source_name": src["name"],
            })
    except Exception as exc:  # noqa: BLE001
        code = f"{src['key'].upper()}_RSS_FAILED"
        errors.append({"code": code, "message": str(exc)[:300]})
        logger.warning("deep_news %s RSS failed: %s", src["key"], exc)
    return items, errors


def _fetch_rss_url_candidates(src: dict[str, Any], limit: int) -> tuple[list[dict], list[dict]]:
    """对多个 RSS URL 顺序尝试，直到拿到条目。"""
    cands = src.get("url_candidates") or ()
    if not cands:
        return _fetch_rss(src, limit)
    all_errors: list[dict[str, Any]] = []
    for u in cands:
        sub = {**src, "url": str(u), "url_candidates": ()}
        items, errs = _fetch_rss(sub, limit)
        all_errors.extend(errs)
        if items:
            return items, all_errors
    return [], all_errors


# ——— 主入口 ——————————————————————————————————————————————————————————

def fetch_deep_news_section(limit: int = 8) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """拉取深度内容层，返回 (section_data, errors)。

    section_data 结构：
      items[]        — 已情感标注的 DeepNewsItem 列表
      sources_tried  — 尝试的源 key 列表
      sources_ok     — 成功返回数据的源 key 列表
      total          — items 总数
      sector_rsshub_matrix — 若配置了 FINANCE_RSSHUB_BASE_URL，为六大板块垂直路由矩阵元数据

    环境变量：
      FINANCE_DEEP_NEWS_PROVIDER=<key>  预留付费 API 接口，当前降级 RSS。
    """
    provider = os.environ.get("FINANCE_DEEP_NEWS_PROVIDER", "").strip()
    if provider:
        if provider in _PROVIDER_DISPATCH:
            try:
                return _PROVIDER_DISPATCH[provider](limit)
            except Exception as exc:  # noqa: BLE001
                logger.warning("FINANCE_DEEP_NEWS_PROVIDER=%s failed (%s)，降级 RSS", provider, exc)
        else:
            logger.info(
                "FINANCE_DEEP_NEWS_PROVIDER=%s 已识别（当前未实装付费接入，降级 RSS/HTTP）", provider
            )

    all_items: list[dict[str, Any]] = []
    all_errors: list[dict[str, Any]] = []
    sources_tried: list[str] = []
    sources_ok: list[str] = []
    sector_matrix: dict[str, Any] | None = None
    base = _rsshub_base()

    per_limit = max(4, limit)
    if base:
        all_items, sector_matrix, route_errs = _fetch_sector_vertical_rsshub(base, limit)
        all_errors.extend(route_errs)
        for sec in SECTOR_ORDER:
            m = (sector_matrix.get("by_sector") or {}).get(sec) or {}
            for d in m.get("routes_tried_detail") or []:
                k = str(d.get("key") or "").strip()
                if k:
                    sources_tried.append(k)
            for k in m.get("routes_ok") or []:
                sources_ok.append(str(k))
        if not all_items:
            logger.warning(
                "deep_news: 六大板块垂直 RSSHub 无条目，降级直连 API/RSS（_BASE_DEEP_SOURCES）",
            )
            for src in _BASE_DEEP_SOURCES:
                sources_tried.append(str(src["key"]))
                if src["type"] == "api_json":
                    items, errs = _fetch_wallstreetcn(src, per_limit)
                elif src.get("url_candidates"):
                    items, errs = _fetch_rss_url_candidates(src, per_limit)
                else:
                    items, errs = _fetch_rss(src, per_limit)
                all_errors.extend(errs)
                if items:
                    sources_ok.append(str(src["key"]))
                    all_items.extend(items)
    else:
        for src in merged_deep_source_list():
            sources_tried.append(src["key"])
            if src["type"] == "api_json":
                items, errs = _fetch_wallstreetcn(src, per_limit)
            elif src.get("url_candidates"):
                items, errs = _fetch_rss_url_candidates(src, per_limit)
            else:
                items, errs = _fetch_rss(src, per_limit)

            all_errors.extend(errs)
            if items:
                sources_ok.append(src["key"])
                all_items.extend(items)

    fresh_days = _fresh_days_limit()
    filtered_items, dropped_stale = _filter_recent_items(all_items, days=fresh_days)
    enriched = _enrich(filtered_items)

    section: dict[str, Any] = {
        "items": enriched,
        "sources_tried": sources_tried,
        "sources_ok": sources_ok,
        "total": len(enriched),
        "rsshub_base_url": base or None,
        "fresh_days_limit": fresh_days,
        "stale_dropped_count": dropped_stale,
    }
    if sector_matrix is not None:
        section["sector_rsshub_matrix"] = sector_matrix
    return section, all_errors
