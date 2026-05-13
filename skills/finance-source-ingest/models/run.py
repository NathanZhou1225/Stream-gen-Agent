"""采集运行记录模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class IngestRun:
    """单次 ingest 执行的统计摘要。"""

    started_at: str = field(default_factory=_now_iso)
    finished_at: str = ""
    sources: str = ""
    keywords: str = ""
    status: str = "running"
    inserted: int = 0
    updated: int = 0
    cleaned: int = 0
    pruned: int = 0
    errors: list[dict] = field(default_factory=list)

    def to_summary(self) -> dict:
        return {
            "ok": self.status == "ok",
            "inserted": self.inserted,
            "updated": self.updated,
            "cleaned": self.cleaned,
            "pruned": self.pruned,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
