"""finance-source-ingest 共用：路径、时间、JSON 输出、原子写。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))

# scripts/ 的上一级 = skill 根目录
SKILL_ROOT = Path(__file__).resolve().parent.parent


def get_skill_root() -> Path:
    return SKILL_ROOT


def get_config_dir() -> Path:
    return SKILL_ROOT / "config"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")
    tmp.replace(path)


def compute_invariants() -> list[str]:
    return [
        "FACTS_FROM_JSON: 数值与条目以 sections + errors 为准，不以 markdown_summary 补数",
        "NO_DRAFTS: 本 skill 不写 drafts/，不与 draft_manager 耦合",
        "MARKET_PRIMARY_AKSHARE: 梯队一主源为 AkShare；海外块仅 FINANCE_SOURCE_OVERSEAS_STUB=1",
    ]


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip() in ("1", "true", "TRUE", "yes", "YES")
