#!/usr/bin/env python3
"""公开热榜聚合拉取。

数据源：
- 主源：tophub.today 首页直抓 HTML（免 key，一次 HTTP 拿全站）
- 备源：HOT_RANK_API_URL 环境变量指定一个 DailyHotApi 兼容的 JSON 端点

任何数据源失败 → 返回 ok=true + 空 lists + errors，不崩（走 BYOD）。

使用：
    python3 fetch_hot_rank.py                       # 默认微博/抖音/百度/知乎
    python3 fetch_hot_rank.py --sites all           # 拉全部（约 80 个站）
    python3 fetch_hot_rank.py --sites "微博,抖音" --top 5
    HOT_RANK_API_URL=https://xxx python3 fetch_hot_rank.py

输出结构见文件底部 SAMPLE_OUTPUT。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any
from urllib import request as _urlreq

from _common import emit_json, now_iso


DEFAULT_SITES = "微博,抖音,百度,知乎"
TOPHUB_URL = "https://tophub.today/"

# tophub 首页解析所需的 regex（验证过 2026-04 版）
CARD_BOUNDARY_RE = re.compile(r'<div class="cc-cd"')
SITE_NAME_RE = re.compile(
    r'<div class="cc-cd-lb">[^<]*<img[^>]*>\s*<span>\s*([^<]+?)\s*</span>',
    re.DOTALL,
)
LIST_NAME_RE = re.compile(r'<span class="cc-cd-sb-st">\s*([^<]+?)\s*</span>')
CARD_SLUG_RE = re.compile(r'<a href="(/n/[^"]+)">')
ITEM_RE = re.compile(
    r'<span class="s[^"]*">\s*(\d+)\s*</span>\s*'
    r'<span class="t">\s*([^<]+?)\s*</span>'
    r'(?:\s*<span class="e">\s*([^<]*?)\s*</span>)?',
    re.DOTALL,
)


def _http_get(url: str, timeout: int = 10) -> str:
    req = _urlreq.Request(url, headers={
        "User-Agent": "Mozilla/5.0 streamy-content-gen/0.1",
        "Accept": "text/html,application/json",
    })
    with _urlreq.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _parse_tophub_home(html: str, top: int) -> list[dict[str, Any]]:
    """解析 tophub.today 首页 HTML，返回 lists[]。"""
    # 切成 cards
    positions = [m.start() for m in CARD_BOUNDARY_RE.finditer(html)]
    lists = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(html)
        card = html[start:end]

        site_m = SITE_NAME_RE.search(card)
        list_m = LIST_NAME_RE.search(card)
        slug_m = CARD_SLUG_RE.search(card)
        if not site_m or not list_m:
            continue

        site = site_m.group(1).replace("&nbsp;", " ").strip()
        list_name = list_m.group(1).strip()
        slug = slug_m.group(1) if slug_m else None

        items = []
        for rank_s, title, hot in ITEM_RE.findall(card):
            try:
                rank = int(rank_s)
            except ValueError:
                continue
            if rank > top:
                continue
            items.append({
                "rank": rank,
                "title": title.strip(),
                "hot_hint": (hot or "").strip() or None,
            })
        if items:
            lists.append({
                "site": site,
                "list_name": list_name,
                "slug_url": f"https://tophub.today{slug}" if slug else None,
                "items": items,
            })
    return lists


def _filter_sites(lists: list[dict[str, Any]], sites_csv: str) -> list[dict[str, Any]]:
    if sites_csv.strip().lower() == "all":
        return lists
    keywords = [s.strip() for s in sites_csv.split(",") if s.strip()]
    if not keywords:
        return lists
    out = []
    for lst in lists:
        site = lst["site"]
        # 子串匹配 + 不区分大小写
        if any(kw in site or kw.lower() in site.lower() for kw in keywords):
            out.append(lst)
    return out


def _try_backup_api(top: int) -> tuple[list[dict[str, Any]], str | None]:
    """备源：HOT_RANK_API_URL 环境变量指向 DailyHotApi 兼容 JSON。

    DailyHotApi 典型响应：
        GET /weibo  → {"code":200, "data":[{"title": "...", "hot":N}]}
    v1 只对 /all 做最简单的尝试；返回 []（由主源负责）。
    """
    url = os.environ.get("HOT_RANK_API_URL", "").strip()
    if not url:
        return [], None
    try:
        raw = _http_get(url, timeout=8)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return [], "HOT_RANK_API_URL 响应非 JSON object"
        # DailyHotApi 风格：data 字段为列表
        items_raw = data.get("data") or []
        if not items_raw:
            return [], "HOT_RANK_API_URL 返回 data 字段空"
        items = []
        for i, x in enumerate(items_raw[:top], start=1):
            items.append({
                "rank": i,
                "title": str(x.get("title", "")).strip(),
                "hot_hint": str(x.get("hot", "") or x.get("desc", "") or "").strip() or None,
            })
        return [{
            "site": data.get("name") or "custom",
            "list_name": data.get("subtitle") or data.get("title") or "api",
            "slug_url": url,
            "items": items,
        }], None
    except Exception as e:  # noqa: BLE001
        return [], f"HOT_RANK_API_URL 失败：{type(e).__name__}: {e}"


def _build_summary(lists: list[dict[str, Any]], top_for_summary: int = 3) -> str:
    if not lists:
        return "热榜：未获取到数据（走 BYOD）"
    parts = []
    for lst in lists[:8]:  # summary 最多展示 8 个站，避免太长
        titles = " / ".join(x["title"] for x in lst["items"][:top_for_summary])
        parts.append(f"• {lst['site']}·{lst['list_name']}：{titles}")
    suffix = f"\n（共 {len(lists)} 个榜单）" if len(lists) > 8 else ""
    return "热榜：\n" + "\n".join(parts) + suffix


def run(sites: str, top: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "command": "fetch_hot_rank",
        "as_of": now_iso(),
        "source": "tophub.today",
        "sites_filter": sites,
        "top": top,
        "lists": [],
        "errors": [],
    }

    # 备源：如果用户设置了 HOT_RANK_API_URL，先尝试它
    backup_lists, backup_err = _try_backup_api(top)
    if backup_lists:
        result["source"] = "HOT_RANK_API_URL"
        result["lists"] = backup_lists
        result["summary"] = _build_summary(backup_lists)
        return result
    if backup_err:
        result["errors"].append({"item": "backup_api", "reason": backup_err})

    # 主源：tophub.today
    try:
        html = _http_get(TOPHUB_URL, timeout=12)
        all_lists = _parse_tophub_home(html, top=top)
        filtered = _filter_sites(all_lists, sites)
        result["lists"] = filtered
        if not filtered:
            result["errors"].append({
                "item": "tophub_filter",
                "reason": f"sites='{sites}' 未匹配任何榜单（抓到 {len(all_lists)} 个站）",
            })
    except Exception as e:  # noqa: BLE001
        result["errors"].append({
            "item": "tophub",
            "reason": f"{type(e).__name__}: {e}",
            "hint": "v1 降级：热榜未获取，走 BYOD（用户粘贴热点）",
        })

    result["summary"] = _build_summary(result["lists"])
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="公开热榜聚合（tophub.today + 可选备源）")
    parser.add_argument(
        "--sites",
        default=DEFAULT_SITES,
        help=f"逗号分隔关键词（子串匹配）或 'all'。默认：{DEFAULT_SITES}",
    )
    parser.add_argument("--top", type=int, default=10, help="每个榜单返回 Top N（默认 10）")
    parser.add_argument("--json", action="store_true", help="兼容性开关（默认 JSON 输出）")
    args = parser.parse_args(argv)

    emit_json(run(sites=args.sites, top=args.top))


if __name__ == "__main__":
    main(sys.argv[1:])


SAMPLE_OUTPUT = """
{
  "ok": true,
  "command": "fetch_hot_rank",
  "as_of": "2026-04-21T17:45:00+08:00",
  "source": "tophub.today",
  "sites_filter": "微博,抖音,百度,知乎",
  "top": 10,
  "lists": [
    {
      "site": "微博",
      "list_name": "热搜榜",
      "slug_url": "https://tophub.today/n/KqndgxeLl9",
      "items": [
        {"rank": 1, "title": "央行降准 0.25 个百分点", "hot_hint": "2800万"}
      ]
    }
  ],
  "errors": [],
  "summary": "..."
}
"""
