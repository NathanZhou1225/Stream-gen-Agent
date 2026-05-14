"""由内容模板 JSON 生成供 LLM structured output 使用的最小 JSON Schema。

无第三方依赖；与 `draft_manager` 的 script.json schema 无关（本处约束「模块键 → 口播段」）。
"""

from __future__ import annotations

import json
from typing import Any


def template_ordered_keys(schema: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for row in schema:
        if not isinstance(row, dict):
            continue
        k = str(row.get("key") or "").strip()
        if k:
            keys.append(k)
    return keys


def build_modules_json_schema(
    keys: list[str],
    *,
    title: str = "script_modules",
    descriptions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Draft-07 object：仅允许模板定义的 string 字段，禁止额外属性。"""
    descriptions = descriptions or {}
    props: dict[str, Any] = {}
    for k in keys:
        desc = descriptions.get(k) or f"口播段落：{k}"
        props[k] = {"type": "string", "description": desc}
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "required": list(keys),
        "properties": props,
    }


def schema_json_text(schema: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(schema, ensure_ascii=False, indent=indent) + "\n"
