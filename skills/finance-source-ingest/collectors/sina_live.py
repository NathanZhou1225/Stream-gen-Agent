"""新浪7x24全球宏观快讯采集器。

内部复用 fetchers/news_sina_live.py（P0 兼容层）。
"""
from __future__ import annotations

import logging

from collectors.base import BaseCollector, CollectorResult
from models.item import RawNewsItem

logger = logging.getLogger(__name__)


class SinaLiveCollector(BaseCollector):
    name = "sina_live"
    handles_sources = ("news", "macro")

    def fetch(self, keywords: list[str] | None = None, max_items: int = 30) -> CollectorResult:
        result = CollectorResult(collector_name=self.name)
        try:
            from fetchers.news_sina_live import fetch_sina_live_section

            section, _errs = fetch_sina_live_section(limit=max(1, max_items))
            items = (section or {}).get("items") or []
            for it in items:
                if not isinstance(it, dict):
                    continue
                title = str(it.get("title") or it.get("clean_text") or "").strip()
                if not title:
                    continue
                if keywords and not any(k in title for k in keywords):
                    continue
                result.news_items.append(
                    RawNewsItem(
                        source=self.name,
                        raw_title=title,
                        raw_content=str(it.get("clean_text") or ""),
                        source_url=str(it.get("url") or ""),
                        published_at=str(it.get("published_at") or it.get("time") or ""),
                        raw_payload=it,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            result.add_error("SINA_LIVE_FAILED", f"{type(exc).__name__}: {exc!s}"[:300])
        return result
