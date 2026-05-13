"""社媒/热搜情绪数据模型。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SentimentHotItem:
    """单条热搜/人气榜条目。"""

    source: str
    keyword: str = ""
    rank: int = 0
    heat: str = ""
    sector: str = ""
    related_stock: str = ""
    snapshot_at: str = field(default_factory=_now_iso)
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def raw_payload_json(self) -> str:
        return json.dumps(self.raw_payload, ensure_ascii=False)
