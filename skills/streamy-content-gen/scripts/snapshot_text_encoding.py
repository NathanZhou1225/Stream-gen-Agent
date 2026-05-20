#!/usr/bin/env python3
"""快照文本编码修复：Windows/错误解码导致的 markdown_summary 乱码检测与恢复。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

# 正常财经快照里常见锚点词
_MARKDOWN_MARKERS = (
    "信源全量快照",
    "大盘与情绪",
    "指数",
    "板块",
    "上证",
    "深证",
    "创业板",
    "情绪",
    "快讯",
)

# 常见 UTF-8↔GBK/Latin1 误读特征
_MOJIBAKE_HINTS = (
    "锟斤拷",
    "锛",
    "堝",
    "缁",
    "閿",
    "銆",
    "鏂",
    "鍙",
    "鎴",
    "鐨",
)


def markdown_quality_score(text: str) -> int:
    if not text:
        return 0
    return sum(1 for m in _MARKDOWN_MARKERS if m in text)


def looks_like_mojibake(text: str, *, min_len: int = 40) -> bool:
    """启发式：财经快照 markdown 应有锚点词；乱码块通常缺失且含典型错字。"""
    if not text or len(text) < min_len:
        return False
    if markdown_quality_score(text) >= 2:
        return False
    for hint in _MOJIBAKE_HINTS:
        if hint in text:
            return True
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk >= min_len * 0.25 and markdown_quality_score(text) == 0:
        return True
    return False


def _try_repair(s: str, encode_as: str) -> str:
    try:
        return s.encode(encode_as, errors="surrogateescape").decode("utf-8", errors="replace")
    except (UnicodeDecodeError, UnicodeEncodeError, LookupError):
        return s


def repair_utf8_mojibake(text: str) -> str:
    """尝试常见误码修复链，取锚点词得分最高者。"""
    if not text:
        return text
    best = text
    best_score = markdown_quality_score(text)
    for enc in ("gbk", "gb18030", "latin-1", "cp1252"):
        candidate = _try_repair(text, enc)
        score = markdown_quality_score(candidate)
        if score > best_score or (score == best_score and len(candidate) > len(best)):
            best, best_score = candidate, score
    return best


def repair_nested_strings(obj: Any) -> Any:
    if isinstance(obj, str):
        if looks_like_mojibake(obj, min_len=8):
            fixed = repair_utf8_mojibake(obj)
            return fixed if markdown_quality_score(fixed) >= markdown_quality_score(obj) else obj
        return obj
    if isinstance(obj, dict):
        return {k: repair_nested_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [repair_nested_strings(v) for v in obj]
    return obj


def _load_db_snapshot_module():
    skill_root = Path(__file__).resolve().parent.parent
    draft_scripts = skill_root.parent / "finance-draft-manager" / "scripts"
    path = draft_scripts / "db_snapshot.py"
    if not path.is_file():
        raise FileNotFoundError(path)
    if str(draft_scripts) not in sys.path:
        sys.path.insert(0, str(draft_scripts))
    spec = importlib.util.spec_from_file_location("db_snapshot_rebuild", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rebuild_markdown_from_snapshot(snap: dict[str, Any]) -> str:
    mod = _load_db_snapshot_module()
    return mod.rebuild_markdown_summary_from_snapshot(snap)


def ensure_snapshot_markdown(snap: dict[str, Any]) -> dict[str, Any]:
    """
    修复 sections 内字符串误码；markdown_summary 乱码时先 repair，仍不行则从 sections 重建。
    """
    out = dict(snap)
    sections = out.get("sections")
    if isinstance(sections, dict) and sections:
        out["sections"] = repair_nested_strings(sections)

    md = str(out.get("markdown_summary") or "")
    meta = dict(out.get("meta") or {})

    if md and not looks_like_mojibake(md):
        return out

    repaired = repair_utf8_mojibake(md) if md else ""
    if repaired and not looks_like_mojibake(repaired):
        out["markdown_summary"] = repaired
        meta["markdown_summary_encoding_repaired"] = True
        out["meta"] = meta
        return out

    if out.get("sections"):
        try:
            out["markdown_summary"] = rebuild_markdown_from_snapshot(out)
            meta["markdown_summary_rebuilt_from_sections"] = True
            out["meta"] = meta
        except Exception as exc:  # noqa: BLE001
            meta["markdown_summary_rebuild_error"] = str(exc)[:200]
            out["meta"] = meta
            if repaired:
                out["markdown_summary"] = repaired
    elif repaired:
        out["markdown_summary"] = repaired

    return out
