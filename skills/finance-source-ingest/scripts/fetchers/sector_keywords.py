"""六大板块关键词过滤（纯 Python，供财联社快讯打标签与分组展示）。"""

from __future__ import annotations

from typing import Any

# 直播侧优先关注的六大板块：科技、新能源、港股、黄金、有色、银行
SECTOR_ORDER = ("科技", "新能源", "港股", "黄金", "有色", "银行")

SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "科技": (
        "科技",
        "人工智能",
        "AI",
        "算力",
        "芯片",
        "半导体",
        "大模型",
        "GPU",
        "光模块",
        "信创",
        "数字经济",
        "互联网",
        "软件",
        "云计算",
        "数据要素",
        "网信",
        "剪映",
        "生成式",
    ),
    "新能源": (
        "新能源",
        "光伏",
        "风电",
        "储能",
        "锂电",
        "电池",
        "充电桩",
        "氢能",
        "新能源车",
        "电动车",
    ),
    "港股": (
        "港股",
        "恒生",
        "南向",
        "北向",
        "港交所",
        "港股市场",
        "恒生科技",
        "恒生指数",
        "南向资金",
    ),
    "黄金": (
        "黄金",
        "贵金属",
        "COMEX",
        "现货黄金",
        "白银",
        "金价",
    ),
    "有色": (
        "有色",
        "铜",
        "铝",
        "锌",
        "镍",
        "稀土",
        "锂矿",
        "工业金属",
        "铁矿石",
    ),
    "银行": (
        "银行",
        "工行",
        "农行",
        "中行",
        "建行",
        "股份行",
        "城商行",
        "农商行",
        "信贷",
        "息差",
        "降息",
        "降准",
        "LPR",
    ),
}


def sectors_for_text(blob: str) -> list[str]:
    """返回本条文本命中的板块列表（可多标签），顺序按 SECTOR_ORDER。"""
    s = blob or ""
    if not s.strip():
        return []
    hit: list[str] = []
    for name in SECTOR_ORDER:
        for kw in SECTOR_KEYWORDS.get(name, ()):
            if kw and kw in s:
                if name not in hit:
                    hit.append(name)
                break
    return hit


def tag_news_item(item: dict[str, Any]) -> dict[str, Any]:
    blob = f"{item.get('title') or ''} {item.get('clean_text') or ''}"
    secs = sectors_for_text(blob)
    out = dict(item)
    out["sector_tags"] = secs
    out["primary_sector"] = secs[0] if secs else ""
    return out


def tag_news_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [tag_news_item(x) if isinstance(x, dict) else x for x in items]


def group_by_sector(
    items: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """按六大板块分组；未命中任何板块的条目进入 ``unclassified``。"""
    grouped: dict[str, list[dict[str, Any]]] = {k: [] for k in SECTOR_ORDER}
    unclassified: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tags = it.get("sector_tags") or []
        if not tags:
            unclassified.append(it)
            continue
        for t in tags:
            if t in grouped and len(grouped[t]) < 12:
                grouped[t].append(it)
    return grouped, unclassified


def unclassified_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [x for x in items if isinstance(x, dict) and not (x.get("sector_tags") or [])]
