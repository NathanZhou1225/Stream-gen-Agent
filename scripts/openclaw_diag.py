"""Single-line OPENCLAW_DIAG helpers for scripts and optional skill imports."""

from __future__ import annotations

import json
from typing import Any, Mapping


def emit_diag(payload: Mapping[str, Any]) -> None:
    line = "OPENCLAW_DIAG " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    print(line, flush=True)


def classify_http_status(status: int | None) -> str:
    if status is None:
        return "NETWORK"
    if status == 401:
        return "AUTH_FAILED"
    if status == 403:
        return "FORBIDDEN"
    if status == 402:
        return "QUOTA_EXCEEDED"
    if status == 429:
        return "RATE_LIMIT"
    if status in (404, 410):
        return "BAD_ENDPOINT"
    if status is not None and 500 <= status <= 599:
        return "UPSTREAM"
    if status is not None and 400 <= status <= 499:
        return "FORBIDDEN"
    return "UNKNOWN"


def diag_from_http(
    *,
    code: str,
    integration: str,
    http_status: int | None,
    env_keys: list[str],
    user_action: str,
    doc_anchor: str,
    safe_hint: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "integration": integration,
        "http_status": http_status,
        "env": env_keys,
        "user_action": user_action,
        "doc_anchor": doc_anchor,
        "safe_hint": safe_hint[:500],
    }
