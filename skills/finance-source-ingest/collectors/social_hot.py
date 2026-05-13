"""社媒热点采集器（微博热搜 → AkShare 淘股吧 → 百度热搜多级降级）。

内部复用 fetchers/social_api.py 与 fetchers/sentiment.py（P0 兼容层）。
"""
from __future__ import annotations

import logging

from collectors.base import BaseCollector, CollectorResult
from models.sentiment import SentimentHotItem

logger = logging.getLogger(__name__)


class SocialHotCollector(BaseCollector):
    name = "social_hot"
    handles_sources = ("social",)

    def fetch(self, keywords: list[str] | None = None, max_items: int = 30) -> CollectorResult:
        result = CollectorResult(collector_name=self.name)
        try:
            from fetchers.social_api import fetch_social_section

            kw = keywords or []
            section, _errs = fetch_social_section(kw, max_items)
            raw = section or {}
            items = (raw or {}).get("items") or []
            for rank, it in enumerate(items, start=1):
                if not isinstance(it, dict):
                    continue
                keyword = str(it.get("keyword") or it.get("title") or "").strip()
                if not keyword:
                    continue
                result.sentiment_items.append(
                    SentimentHotItem(
                        source=self.name,
                        keyword=keyword,
                        rank=rank,
                        heat=str(it.get("heat") or ""),
                        sector=str(it.get("sector") or ""),
                        related_stock=str(it.get("stock") or ""),
                        raw_payload=it,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            result.add_error("SOCIAL_HOT_FAILED", f"{type(exc).__name__}: {exc!s}"[:300])
        return result
