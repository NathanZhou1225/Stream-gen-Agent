"""证监会 / 央行公告列表 + 可选 Tushare CCTV 新闻联播摘要（政策与大事件补充）。"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

CSRC_INDEX = "https://www.csrc.gov.cn/csrc/c100028/index.shtml"
PBC_INDEX = "https://www.pbc.gov.cn/goutongjiaoliu/113456/index.html"

_USER_AGENT = "Mozilla/5.0 (compatible; finance-source-ingest/0.3)"


def _decode_html(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _fetch_html(url: str, timeout: int = 20) -> str:
    req = urlrequest.Request(url, headers={"User-Agent": _USER_AGENT})
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return _decode_html(resp.read())


def _abs_url(base: str, href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith("#") or href.startswith("javascript:"):
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urlparse.urljoin(base, href)


def _parse_csrc(html: str, base_url: str, limit: int) -> list[dict[str, Any]]:
    """从证监会列表页提取标题与链接。"""
    items: list[dict[str, Any]] = []
    # 常见：<a href="/csrc/c100028/c100029/xxx/title.html" target="_blank">标题</a>
    # 或带 class 的列表项
    pat = re.compile(
        r'href="([^"]*(?:/csrc/c100028/|c100028)[^"]*\.(?:html|shtml))"[^>]*>([^<]{4,200})</a>',
        re.I,
    )
    seen: set[str] = set()
    for m in pat.finditer(html):
        href, title = m.group(1).strip(), _clean_title(m.group(2))
        if not title or len(title) < 4:
            continue
        full = _abs_url(base_url, href)
        if not full or full in seen:
            continue
        seen.add(full)
        date_s = _nearby_date(html, m.start()) or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        items.append({
            "title": title,
            "published_at": f"{date_s}T00:00:00+08:00" if len(date_s) == 10 else date_s,
            "url": full,
            "source_name": "证监会",
            "source_kind": "policy",
        })
        if len(items) >= limit:
            break
    return items


def _clean_title(s: str) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip())
    t = re.sub(r"<[^>]+>", "", t)
    return t.strip()


def _nearby_date(html: str, pos: int) -> str:
    """在链接前 400 字符内找 YYYY-MM-DD。"""
    start = max(0, pos - 400)
    chunk = html[start:pos]
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", chunk)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{y}-{mo:02d}-{d:02d}"
    return ""


def _parse_pbc(html: str, base_url: str, limit: int) -> list[dict[str, Any]]:
    """央行沟通与交流栏目列表。"""
    items: list[dict[str, Any]] = []
    pat = re.compile(
        r'href="([^"]*(?:/goutongjiaoliu/113456/)[^"]*\.html)"[^>]*>([^<]{4,200})</a>',
        re.I,
    )
    seen: set[str] = set()
    for m in pat.finditer(html):
        href, title = m.group(1).strip(), _clean_title(m.group(2))
        if not title or "首页" in title or "更多" in title:
            continue
        full = _abs_url(base_url, href)
        if not full or full in seen:
            continue
        seen.add(full)
        date_s = _nearby_date(html, m.start()) or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        items.append({
            "title": title,
            "published_at": f"{date_s}T00:00:00+08:00" if len(date_s) == 10 else date_s,
            "url": full,
            "source_name": "人民银行",
            "source_kind": "policy",
        })
        if len(items) >= limit:
            break
    return items


def _fetch_tushare_cctv(limit: int) -> list[dict[str, Any]]:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        return []
    try:
        import tushare as ts  # noqa: PLC0415
    except ImportError:
        logger.info("tushare 未安装，跳过 CCTV 新闻")
        return []
    out: list[dict[str, Any]] = []
    try:
        pro = ts.pro_api(token)
        tz = timezone(timedelta(hours=8))
        for delta in range(0, 4):
            if len(out) >= limit:
                break
            day = (datetime.now(tz) - timedelta(days=delta)).strftime("%Y%m%d")
            df = pro.cctv_news(date=day)
            if df is None or getattr(df, "empty", True):
                continue
            remain = limit - len(out)
            for _, row in df.head(remain).iterrows():
                title = str(row.get("title") or "").strip()
                if not title:
                    continue
                content = str(row.get("content") or "").strip()
                date_raw = str(row.get("date") or day)
                if len(date_raw) == 8 and date_raw.isdigit():
                    pub = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}T19:00:00+08:00"
                elif len(date_raw) >= 10:
                    pub = f"{date_raw[:10]}T19:00:00+08:00"
                else:
                    pub = datetime.now(tz).isoformat(timespec="seconds")
                ct = (content[:400] + "…") if len(content) > 400 else content
                out.append({
                    "title": title[:200],
                    "clean_text": ct,
                    "published_at": pub,
                    "url": "",
                    "source_name": "CCTV新闻联播",
                    "source_kind": "policy",
                })
    except Exception as exc:  # noqa: BLE001
        logger.warning("tushare cctv_news failed: %s", exc)
    return out


def fetch_policy_section(
    *,
    csrc_limit: int = 5,
    pbc_limit: int = 5,
    cctv_limit: int = 5,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """返回 (section_data, errors)。无 Tushare token 时静默跳过 CCTV。"""
    errors: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []

    try:
        html_c = _fetch_html(CSRC_INDEX)
        all_items.extend(_parse_csrc(html_c, CSRC_INDEX, csrc_limit))
    except (urlerror.URLError, OSError, TimeoutError, ValueError) as exc:
        logger.warning("csrc fetch failed: %s", exc)
        errors.append({
            "source": "policy_gov",
            "code": "CSRC_INDEX_FAILED",
            "message": str(exc)[:300],
        })

    try:
        html_p = _fetch_html(PBC_INDEX)
        all_items.extend(_parse_pbc(html_p, PBC_INDEX, pbc_limit))
    except (urlerror.URLError, OSError, TimeoutError, ValueError) as exc:
        logger.warning("pbc fetch failed: %s", exc)
        errors.append({
            "source": "policy_gov",
            "code": "PBC_INDEX_FAILED",
            "message": str(exc)[:300],
        })

    cctv_items = _fetch_tushare_cctv(cctv_limit)
    all_items.extend(cctv_items)

    # 按 published_at 字符串排序（降序近似）
    def _sort_key(it: dict[str, Any]) -> str:
        return str(it.get("published_at") or "")

    all_items.sort(key=_sort_key, reverse=True)

    return {
        "items": all_items,
        "total": len(all_items),
        "sources": {
            "csrc": CSRC_INDEX,
            "pbc": PBC_INDEX,
            "cctv": "tushare" if cctv_items else None,
        },
    }, errors
