"""财联社电报/快讯采集器（RSSHub 路由优先，回落直连）。

内部复用 fetchers/news_rss.py（P0 兼容层）。
"""
from __future__ import annotations

import logging

from collectors.base import BaseCollector, CollectorResult
from models.item import RawNewsItem

logger = logging.getLogger(__name__)


class ClsTelegraphCollector(BaseCollector):
    name = "cls_telegraph"
    handles_sources = ("news",)

    def fetch(self, keywords: list[str] | None = None, max_items: int = 30) -> CollectorResult:
        result = CollectorResult(collector_name=self.name)
        try:
            from fetchers.news_rss import fetch_news_section

            kw = keywords or []
            section, _errs = fetch_news_section(kw, max_items)
            raw = section or {}
            items = (raw or {}).get("items") or []
            for it in items:
                if not isinstance(it, dict):
                    continue
                title = str(it.get("title") or "").strip()
                if not title:
                    continue
                if keywords and not any(k in title for k in keywords):
                    continue
                result.news_items.append(
                    RawNewsItem(
                        source=self.name,
                        raw_title=title,
                        raw_content=str(it.get("clean_text") or it.get("summary") or ""),
                        source_url=str(it.get("url") or ""),
                        published_at=str(it.get("published_at") or it.get("time") or ""),
                        raw_payload=it,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            result.add_error("CLS_TELEGRAPH_FAILED", f"{type(exc).__name__}: {exc!s}"[:300])
        return result
