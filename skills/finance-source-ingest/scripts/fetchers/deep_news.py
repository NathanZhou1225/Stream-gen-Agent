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

from .sector_keywords import sectors_for_text
from .sentiment import classify_impact, classify_sentiment, extract_stock_mentions, sentiment_emoji

logger = logging.getLogger(__name__)

# ——— 深度内容源配置 ——————————————————————————————————————————————————————

_SUMMARY_MAX_LEN = 120  # 约 2-3 句核心信息，与选题 digest 上限对齐

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
    ("wallstreetcn_rsshub", "华尔街见闻(RSSHub)", "/wallstreetcn/live"),
    ("jin10_rsshub", "金十数据(RSSHub)", "/jin10"),
    ("yicai_rsshub", "第一财经(RSSHub)", "/yicai/brief"),
    ("jiemian_rsshub", "界面快报(RSSHub)", "/jiemian/lists/4"),
    ("kr36_rsshub", "36氪快讯(RSSHub)", "/36kr/newsflashes"),
)


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
        enriched["sector_tags"] = sectors_for_text(text)
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
    summary = _clip_summary(str(body) if body else title)
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
            summary = _clip_summary(raw_summary)
            link = str(getattr(entry, "link", "") or "").strip()
            pub_raw = (
                getattr(entry, "published", "")
                or getattr(entry, "updated", "")
                or ""
            )
            items.append({
                "title": title,
                "summary": summary,
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

    per_limit = max(4, limit)
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

    enriched = _enrich(all_items)

    return {
        "items": enriched,
        "sources_tried": sources_tried,
        "sources_ok": sources_ok,
        "total": len(enriched),
        "rsshub_base_url": _rsshub_base() or None,
    }, all_errors
