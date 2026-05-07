"""宏观/泛财经热搜（优先 urllib + 公开接口，避免被封爬虫封装）。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib import request as urlrequest

from _common import now_iso

logger = logging.getLogger(__name__)

# 金融相关弱过滤（命中其一则认为与财经相关）
_FINANCE_HINT = re.compile(
    r"(股|债|汇|金|银|期|涨|跌|央行|美联储|GDP|PMI|通胀|降息|加息|"
    r"港股|A股|美股|外资|融资|并购|财报|业绩|银行|地产|原油|黄金|美元|人民币)",
)


def _fetch_bytes(url: str, *, timeout: float = 10.0) -> bytes | None:
    req = urlrequest.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; finance-source-ingest/0.2; +cloud)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:  # noqa: BLE001
        logger.debug("macro_hot fetch failed %s: %s", url, e)
        return None


def fetch_baidu_realtime_hot(*, limit: int = 15) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    百度热搜榜（公开 JSON API，常见可被服务端访问）。
    返回 (词条列表, errors)。
    """
    errors: list[dict[str, Any]] = []
    url = "https://top.baidu.com/api/board?platform=wise&tab=realtime"
    raw = _fetch_bytes(url, timeout=12.0)
    if not raw:
        errors.append(
            {
                "source": "macro_hot",
                "stage": "baidu_board",
                "code": "MACRO_HOT_FETCH_FAILED",
                "message": "empty response",
                "hint": url,
            }
        )
        return [], errors
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        errors.append(
            {
                "source": "macro_hot",
                "stage": "baidu_board",
                "code": "MACRO_HOT_JSON",
                "message": str(e),
                "hint": url,
            }
        )
        return [], errors

    cards = (((data or {}).get("data") or {}).get("cards")) or []
    out: list[dict[str, Any]] = []

    def _walk_for_words(obj: Any, acc: list[str], depth: int = 0) -> None:
        if depth > 14 or len(acc) >= limit * 3:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("word", "query", "title") and isinstance(v, str):
                    t = v.strip()
                    if t and t not in acc:
                        acc.append(t)
                else:
                    _walk_for_words(v, acc, depth + 1)
        elif isinstance(obj, list):
            for el in obj:
                _walk_for_words(el, acc, depth + 1)

    for card in cards:
        if not isinstance(card, dict):
            continue
        content = card.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("component") != "hotList":
                continue
            items = block.get("content") or []
            if not isinstance(items, list):
                continue
            for row in items:
                if not isinstance(row, dict):
                    continue
                wd = str(row.get("word") or row.get("query") or "").strip()
                if not wd:
                    continue
                idx = row.get("index")
                detail = ""
                skip_keys = {"word", "query", "title", "index", "url", "link", "uri", "icon", "img", "image", "rawUrl"}
                for k, v in row.items():
                    if k in skip_keys or not isinstance(v, str):
                        continue
                    t = v.strip()
                    if len(t) > len(detail) and len(t) > len(wd) + 2:
                        detail = t
                out.append(
                    {
                        "rank": idx,
                        "title": wd,
                        "detail": detail[:400] if detail else "",
                        "source": "baidu:realtime",
                    }
                )
                if len(out) >= max(limit, 5):
                    break
            if len(out) >= max(limit, 5):
                break
        if len(out) >= max(limit, 5):
            break

    if not out:
        flat_words: list[str] = []
        _walk_for_words(data, flat_words)
        for i, wd in enumerate(flat_words[: max(limit, 8)]):
            out.append({"rank": i + 1, "title": wd, "detail": "", "source": "baidu:realtime:walk"})

    # 财经相关过滤
    fin: list[dict[str, Any]] = []
    for x in out[: limit * 2]:
        t = x.get("title") or ""
        if _FINANCE_HINT.search(t):
            fin.append(x)
        if len(fin) >= limit:
            break

    if not fin and out:
        errors.append(
            {
                "source": "macro_hot",
                "stage": "baidu_board",
                "code": "MACRO_HOT_FINANCE_FILTER_EMPTY",
                "message": "财经关键词过滤无命中，本轮不展示百度非财经热榜。",
                "hint": str(len(out)),
            }
        )

    return fin[:limit], errors


def fetch_macro_section(*, limit: int = 12) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    items, errs = fetch_baidu_realtime_hot(limit=limit)
    return (
        {
            "source_primary": "baidu:board_api",
            "as_of": now_iso(),
            "items": items,
        },
        errs,
    )
