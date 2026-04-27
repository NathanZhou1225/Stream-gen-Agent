"""大盘指数单行契约：主链路 (AkShare) 与降级链路 (新浪) 输出结构必须一致。"""

from __future__ import annotations

from typing import TypedDict


class AShareIndexItem(TypedDict):
    """a_share_indices.items[] 中每一条的结构。"""

    code: str
    name: str
    close: float | None
    pct_change: float | None


class HotStockItem(TypedDict):
    """market_sentiment.top_hot_stocks[] 中每一条的结构。"""

    name: str
    code: str


class MarketSentiment(TypedDict):
    """
    sections["market"].market_sentiment 数据结构契约。"""

    hot_keywords: list[str]  # 东方财富热词榜 Top5（纯文本）
    top_hot_stocks: list[HotStockItem]  # 东方财富人气股 Top3


def normalize_index_item(
    *,
    code: str,
    name: str,
    close: float | None,
    pct_change: float | None,
) -> AShareIndexItem:
    return {
        "code": code,
        "name": name,
        "close": close,
        "pct_change": pct_change,
    }
