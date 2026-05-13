"""统一新闻/快讯数据模型。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _make_dedupe_key(source: str, source_url: str, title: str, published_at: str) -> str:
    """
    基于 source + URL（优先）或 title + published_at 生成稳定去重键。
    同一 URL 来自不同抓取轮次视为同一条；无 URL 时按 title+时间哈希。
    """
    if source_url and source_url.strip():
        raw = f"{source}::{source_url.strip()}"
    else:
        t = (title or "").strip()[:120]
        p = (published_at or "").strip()[:19]
        raw = f"{source}::{t}::{p}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class RawNewsItem:
    """采集器输出的原始新闻条目，进入 storage 前保持不可变。"""

    source: str
    raw_title: str
    raw_content: str = ""
    source_url: str = ""
    published_at: str = ""
    fetched_at: str = field(default_factory=_now_iso)
    raw_payload: dict[str, Any] = field(default_factory=dict)

    # 由 storage.py 在 upsert 后填充
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        if not self.dedupe_key:
            self.dedupe_key = _make_dedupe_key(
                self.source,
                self.source_url,
                self.raw_title,
                self.published_at,
            )

    def raw_payload_json(self) -> str:
        return json.dumps(self.raw_payload, ensure_ascii=False)


@dataclass
class CleanedFields:
    """LLM 清洗层产出，用于更新 news_items 的 normalized 字段。"""

    dedupe_key: str
    clean_title: str = ""
    clean_summary: str = ""
    sector: str = ""
    sentiment: str = ""
    importance_score: float = 0.0
    tags: list[str] = field(default_factory=list)
    llm_clean_model: str = ""

    def tags_json(self) -> str:
        return json.dumps(self.tags, ensure_ascii=False)
