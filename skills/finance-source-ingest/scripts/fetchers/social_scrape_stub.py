"""社媒自爬占位：默认不实现 Playwright，仅返回契约预览。"""

from __future__ import annotations

import os
from typing import Any

from _common import now_iso, truthy_env


def fetch_social_scrape_stub() -> dict[str, Any]:
    if not truthy_env("ENABLE_SOCIAL_SCRAPE"):
        return {
            "enabled": False,
            "note": "自爬未启用。设置 ENABLE_SOCIAL_SCRAPE=1 仅返回 schema 预览，仍不发起浏览器抓取（v0.1）。",
        }
    return {
        "enabled": True,
        "implementation": "DISABLED_IMPLEMENTATION",
        "as_of": now_iso(),
        "schema_preview": {
            "items": [
                {
                    "platform": "bilibili|xiaohongshu|douyin",
                    "title": "str",
                    "url": "str",
                    "rank": "int?",
                    "heat": "str?",
                }
            ]
        },
        "note": "v0.1 不提供真实自爬；请使用第三方 API 或后续版本接入 Playwright/DrissionPage。",
    }
