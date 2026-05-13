"""RSSHub 六大板块深度资讯采集器。

内部复用 fetchers/deep_news.py（P0 兼容层）。
"""
from __future__ import annotations

import logging

from collectors.base import BaseCollector, CollectorResult
from models.item import RawNewsItem

logger = logging.getLogger(__name__)


class RSSHubCollector(BaseCollector):
    name = "rsshub"
    handles_sources = ("news", "deep")

    def fetch(self, keywords: list[str] | None = None, max_items: int = 30) -> CollectorResult:
        result = CollectorResult(collector_name=self.name)
        try:
            from fetchers.deep_news import fetch_deep_news_section

            lim = max(4, min(max_items, 48))
            section, _errs = fetch_deep_news_section(limit=lim)
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
            result.add_error("RSSHUB_FAILED", f"{type(exc).__name__}: {exc!s}"[:300])
        return result
