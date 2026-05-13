"""证监会/人民银行公告采集器。

内部复用 fetchers/policy_gov.py（P0 兼容层）。
"""
from __future__ import annotations

import logging

from collectors.base import BaseCollector, CollectorResult
from models.item import RawNewsItem

logger = logging.getLogger(__name__)


class PolicyGovCollector(BaseCollector):
    name = "policy_gov"
    handles_sources = ("news", "policy")

    def fetch(self, keywords: list[str] | None = None, max_items: int = 30) -> CollectorResult:
        result = CollectorResult(collector_name=self.name)
        try:
            from fetchers.policy_gov import fetch_policy_section

            n = max(3, min(max_items, 12))
            section, _errs = fetch_policy_section(csrc_limit=n, pbc_limit=n, cctv_limit=n)
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
                        raw_content=str(it.get("content") or it.get("summary") or ""),
                        source_url=str(it.get("url") or ""),
                        published_at=str(it.get("published_at") or ""),
                        raw_payload=it,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            result.add_error("POLICY_GOV_FAILED", f"{type(exc).__name__}: {exc!s}"[:300])
        return result
