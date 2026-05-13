"""BaseCollector 抽象基类（Finance Newsbox 模式）。

每个信源对应一个 Collector，实现 fetch() 返回统一模型列表。
调度器（ingest.py）并发调用所有启用的 Collector，结果统一入库。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from models.item import RawNewsItem
from models.market import MarketSnapshot
from models.sentiment import SentimentHotItem

logger = logging.getLogger(__name__)


@dataclass
class CollectorResult:
    """单个 Collector 的抓取结果容器。"""

    collector_name: str
    news_items: list[RawNewsItem] = field(default_factory=list)
    market_items: list[MarketSnapshot] = field(default_factory=list)
    sentiment_items: list[SentimentHotItem] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True

    def add_error(self, code: str, message: str, stage: str = "fetch") -> None:
        self.errors.append(
            {"collector": self.collector_name, "stage": stage, "code": code, "message": message}
        )
        self.ok = False


class BaseCollector(ABC):
    """
    所有采集器的抽象基类。

    子类必须实现 fetch()，返回 CollectorResult。
    fetch() 内部出现异常时应捕获并调用 result.add_error()，
    保证调度器不因单个 Collector 失败整体中断。
    """

    #: 唯一标识符，用于 source_state 记录与日志
    name: str = "base"

    #: 该 Collector 可处理的 --sources 参数值（如 "news", "market", "social"）
    handles_sources: tuple[str, ...] = ()

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config or {}
        self._logger = logging.getLogger(f"collector.{self.name}")

    @abstractmethod
    def fetch(self, keywords: list[str] | None = None, max_items: int = 30) -> CollectorResult:
        """
        执行抓取，返回 CollectorResult。

        :param keywords: 可选关键词过滤（由调度器传入）。
        :param max_items: 最大条目数限制。
        :return: CollectorResult（含 news_items / market_items / errors）
        """

    def is_enabled(self, sources: list[str]) -> bool:
        """根据 sources 参数判断本 Collector 是否应运行。"""
        if not sources or "all" in sources:
            return True
        return any(s in sources for s in self.handles_sources)

    def _safe_fetch(
        self,
        keywords: list[str] | None = None,
        max_items: int = 30,
    ) -> CollectorResult:
        """
        带异常捕获的 fetch 包装，供调度器统一调用。
        子类不应覆盖此方法；覆盖 fetch() 即可。
        """
        result = CollectorResult(collector_name=self.name)
        try:
            result = self.fetch(keywords=keywords, max_items=max_items)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("[%s] fetch failed: %s", self.name, exc, exc_info=True)
            result.add_error(
                code="FETCH_EXCEPTION",
                message=f"{type(exc).__name__}: {exc!s}"[:400],
            )
        return result
