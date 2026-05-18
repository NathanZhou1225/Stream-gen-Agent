"""新浪三大指数 + AkShare 北向/行业/涨跌停 采集器。

内部复用 fetchers/market.py（P0 兼容层），P1 可吸收后删除 fetchers。
"""
from __future__ import annotations

import logging
from typing import Any

from collectors.base import BaseCollector, CollectorResult
from models.market import MarketSnapshot

logger = logging.getLogger(__name__)


class SinaMarketCollector(BaseCollector):
    name = "sina_market"
    handles_sources = ("market",)

    def fetch(self, keywords: list[str] | None = None, max_items: int = 30) -> CollectorResult:
        result = CollectorResult(collector_name=self.name)
        try:
            # P0 兼容层：复用已验证的 fetchers/market.py
            from fetchers.market import fetch_market_section

            raw, _errs = fetch_market_section(overseas_stub_enabled=False)
            indices = (raw.get("a_share_indices") or {}).get("items") or []
            for idx_item in indices:
                if not isinstance(idx_item, dict):
                    continue
                price = _first_float(idx_item, "close", "current", "price")
                change_pct = _first_float(idx_item, "pct_change", "percent", "change_pct")
                snap = MarketSnapshot(
                    index_code=str(idx_item.get("code") or idx_item.get("symbol") or ""),
                    index_name=str(idx_item.get("name") or ""),
                    price=price,
                    change_pct=change_pct,
                    raw_payload=idx_item,
                )
                # 开盘瞬间新浪常返回 0.0 占位；勿入库以免覆盖上一档有效快照
                if snap.index_code and price is not None and price > 0:
                    result.market_items.append(snap)
        except Exception as exc:  # noqa: BLE001
            result.add_error("SINA_MARKET_FAILED", f"{type(exc).__name__}: {exc!s}"[:300])
        return result


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_float(item: dict[str, Any], *keys: str) -> float | None:
    """取第一个已存在字段（0.0 为有效值，勿用 ``or`` 链）。"""
    for key in keys:
        if key not in item:
            continue
        val = _to_float(item.get(key))
        if val is not None:
            return val
    return None
