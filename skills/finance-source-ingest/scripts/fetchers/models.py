"""数据契约：主链路与降级链路输出结构必须一致；深度内容层新增 DeepNewsItem。"""

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


class SectorInflowItem(TypedDict):
    """market_temperature.top_inflow_sectors[] 中每一条的结构。"""

    name: str
    main_net_inflow_yi: float | None


class MarketTemperature(TypedDict):
    """
    sections["market"].market_temperature 数据结构契约。"""

    source: str
    top_inflow_sectors: list[SectorInflowItem]
    limit_up_count: int | None
    limit_down_count: int | None


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


# ——— 深度内容层契约 ————————————————————————————————————————————————————

class DeepNewsItem(TypedDict, total=False):
    """sections["deep_news"]["items"][] 中每一条的结构。

    title / summary / source / published_at 为必填；其余由 sentiment.enrich_item 补充。
    """

    title: str
    summary: str          # 2-3 句核心信息，≤120 字，已去 HTML
    url: str
    published_at: str     # ISO8601（Asia/Shanghai）
    source: str           # "wallstreetcn" | "yicai" | "jiemian"
    source_name: str      # "华尔街见闻" | "第一财经" | "界面新闻"
    sector_tags: list     # 命中的六大板块列表
    sentiment_hint: str   # "利好" | "利空" | "中性"（规则 based）
    sentiment_emoji: str  # "🟢" | "🔴" | "⚪"
    impact_level: str     # "市场" | "行业" | "公司"（规则 based）
    stock_mentions: list  # 提及的已知股票/公司名称列表


class SentimentEnrichedNewsItem(TypedDict, total=False):
    """CLS 快讯回填情感字段后的扩展结构（非破坏性，原有字段不变）。"""

    sentiment_hint: str   # "利好" | "利空" | "中性"
    sentiment_emoji: str  # "🟢" | "🔴" | "⚪"
    impact_level: str     # "市场" | "行业" | "公司"
    stock_mentions: list  # 提及的已知股票/公司名称列表
