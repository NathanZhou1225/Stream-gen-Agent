"""新浪财经 7x24 全球实时快讯（JSON API，与 hq.sinajs.cn 同域系，机房友好）。

用于「全球宏观」板块；仅输出与金融/宏观相关的条目（关键词过滤）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

# 与 pipeline.FINANCE_TEXT_HINTS 对齐（避免 fetcher → pipeline 循环 import）
_FINANCE_FILTER_KEYWORDS: tuple[str, ...] = (
    "A股",
    "港股",
    "美股",
    "股市",
    "股票",
    "股价",
    "股东",
    "板块",
    "债券",
    "国债",
    "汇率",
    "期货",
    "涨停",
    "跌停",
    "涨幅",
    "跌幅",
    "上涨",
    "下跌",
    "央行",
    "美联储",
    "GDP",
    "PMI",
    "通胀",
    "降息",
    "加息",
    "外资",
    "融资",
    "并购",
    "财报",
    "业绩",
    "银行",
    "地产",
    "原油",
    "黄金",
    "白银",
    "贵金属",
    "美元",
    "人民币",
    "产业",
    "出口",
    "进口",
    "关税",
    "国资",
    "财政政策",
    "特别国债",
    "金融",
    "产权市场",
    "交易额",
    "资金",
    "成交",
    "指数",
    "矿产",
    "大宗商品",
    "联储",
    "非农",
    "CPI",
    "PPI",
    "美债",
    "日元",
    "欧元",
    "英镑",
    "油价",
    "OPEC",
    "地缘",
    "制裁",
    "俄乌",
    "中东",
    "以色列",
    "伊朗",
    "欧盟",
    "G7",
    "G20",
    "IMF",
    "世界银行",
    "证监会",
    "发改委",
    "财政部",
    "商务部",
    "国务院",
    "国常会",
    "降准",
    "LPR",
    "鲍威尔",
    "耶伦",
    "美联储",
    "华尔街",
    "纳指",
    "道指",
    "标普",
    "恒生",
    "南向",
    "北向",
    "沪深",
    "两市",
    "成交额",
    "放量",
    "缩量",
)

_SINA_FEED_URL = (
    "https://zhibo.sina.com.cn/api/zhibo/feed"
    "?page=1&page_size=30&zhibo_id=152"
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    t = _HTML_TAG_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", t).strip()


def _is_finance_related(text: str) -> bool:
    blob = text or ""
    return any(k and k in blob for k in _FINANCE_FILTER_KEYWORDS)


def _parse_create_time(raw: Any) -> str:
    """归一化为 Asia/Shanghai ISO8601。"""
    tz_cn = timezone(timedelta(hours=8))
    if raw is None:
        return datetime.now(tz_cn).isoformat(timespec="seconds")
    if isinstance(raw, (int, float)):
        ts = int(raw)
        if 1_000_000_000 < ts < 9_999_999_999:
            return datetime.fromtimestamp(ts, tz=tz_cn).isoformat(timespec="seconds")
    s = str(raw).strip()
    if not s:
        return datetime.now(tz_cn).isoformat(timespec="seconds")
    if s.isdigit() and len(s) >= 10:
        try:
            return datetime.fromtimestamp(int(s), tz=tz_cn).isoformat(timespec="seconds")
        except (OSError, ValueError):
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt.replace(tzinfo=tz_cn).isoformat(timespec="seconds")
        except ValueError:
            continue
    return s[:19] if len(s) >= 16 else datetime.now(tz_cn).isoformat(timespec="seconds")


def _extract_feed_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容多种嵌套路径。"""
    cand: list[Any] = []
    r = data.get("result")
    if isinstance(r, dict):
        d = r.get("data")
        if isinstance(d, dict):
            feed = d.get("feed")
            if isinstance(feed, dict):
                lst = feed.get("list")
                if isinstance(lst, list):
                    cand = lst
    if not cand and isinstance(data.get("data"), dict):
        feed = data["data"].get("feed")
        if isinstance(feed, dict) and isinstance(feed.get("list"), list):
            cand = feed["list"]
    out: list[dict[str, Any]] = []
    for x in cand:
        if isinstance(x, dict):
            out.append(x)
    return out


def fetch_sina_live_section(limit: int = 12) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """拉取新浪 7x24，过滤金融相关，返回 (section_data, errors)。

    section_data:
      items[]     — title, clean_text, published_at, source, source_name
      total       — 条数
      source_url  — 请求 URL
    """
    errors: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; finance-source-ingest/0.3)",
        "Accept": "application/json",
        "Referer": "https://finance.sina.com.cn/",
    }
    try:
        req = urlrequest.Request(_SINA_FEED_URL, headers=headers)
        with urlrequest.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except (urlerror.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("sina live feed failed: %s", exc)
        errors.append({
            "source": "news_sina_live",
            "code": "SINA_LIVE_FEED_FAILED",
            "message": str(exc)[:300],
        })
        return {
            "items": [],
            "total": 0,
            "source_url": _SINA_FEED_URL,
        }, errors

    if not isinstance(data, dict):
        errors.append({
            "source": "news_sina_live",
            "code": "SINA_LIVE_SHAPE",
            "message": "响应非 JSON 对象",
        })
        return {"items": [], "total": 0, "source_url": _SINA_FEED_URL}, errors

    feed_list = _extract_feed_list(data)
    if not feed_list:
        errors.append({
            "source": "news_sina_live",
            "code": "SINA_LIVE_EMPTY",
            "message": "feed.list 为空或路径变更",
        })
        return {"items": [], "total": 0, "source_url": _SINA_FEED_URL}, errors

    seen: set[str] = set()
    for row in feed_list:
        if len(items) >= max(1, limit):
            break
        rich = row.get("rich_text") or row.get("content") or row.get("text") or ""
        clean = _strip_html(str(rich))
        if not clean or len(clean) < 8:
            continue
        if not _is_finance_related(clean):
            continue
        key = clean[:80]
        if key in seen:
            continue
        seen.add(key)
        ts_raw = row.get("create_time") or row.get("ctime") or row.get("time")
        published = _parse_create_time(ts_raw)
        items.append({
            "title": clean[:120] + ("…" if len(clean) > 120 else ""),
            "clean_text": clean[:400] + ("…" if len(clean) > 400 else ""),
            "published_at": published,
            "source": "sina_live",
            "source_name": "新浪财经7x24",
        })

    if not items:
        errors.append({
            "source": "news_sina_live",
            "code": "SINA_LIVE_FINANCE_FILTER_EMPTY",
            "message": "7x24 有数据但无条目命中金融关键词过滤",
        })

    return {
        "items": items,
        "total": len(items),
        "source_url": _SINA_FEED_URL,
    }, errors
