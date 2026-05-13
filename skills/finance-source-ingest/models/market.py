"""大盘行情快照数据模型。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MarketSnapshot:
    """单个指数/行情快照条目。"""

    index_code: str
    index_name: str = ""
    price: float | None = None
    change_pct: float | None = None
    volume: float | None = None
    turnover: float | None = None
    snapshot_at: str = field(default_factory=_now_iso)
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def raw_payload_json(self) -> str:
        return json.dumps(self.raw_payload, ensure_ascii=False)
