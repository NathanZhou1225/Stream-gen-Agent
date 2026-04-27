#!/usr/bin/env python3
"""财联社电报拉取（v1 stub 实现）。

🚧 v1 刻意只做 stub：PRD 说明 v1 不实装真实接入，等业务方提供凭据后再填。

使用：
    python3 fetch_cls_telegram.py --json

行为：
    - 若 CLS_TELEGRAM_TOKEN 未设置 → 返回 ok=true, items=[]（让 Agent 走 BYOD 路径）
    - 若设置了 → 当前仍返回 not_implemented，提示业务方联系 skill 维护者对接

输出（stub 默认路径）：
    {
      "ok": true,
      "command": "fetch_cls_telegram",
      "result": {
        "items": [],
        "note": "财联社电报接口 v1 未实装，走 BYOD 路径。"
      }
    }
"""

from __future__ import annotations

import argparse
import os
import sys

from _common import emit_ok


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="财联社电报拉取 (v1 stub)")
    parser.add_argument("--top", type=int, default=20, help="拉取最新 N 条（未实装时忽略）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    token = os.environ.get("CLS_TELEGRAM_TOKEN", "").strip()

    if not token:
        emit_ok(
            command="fetch_cls_telegram",
            result={
                "items": [],
                "note": "未配置 CLS_TELEGRAM_TOKEN，stub 返回空。请 Agent 走 BYOD 路径。",
            },
            summary="财联社电报：未配置，返回空。",
        )
        return

    # Token 已设置但 v1 不接入真实 API —— 明确返回 ok=true + stub 字段结构，
    # 便于 v2 对接时不改调用侧；同时在 note 里说清楚下一步。
    emit_ok(
        command="fetch_cls_telegram",
        result={
            "items": [],
            "schema_preview": {
                "items": [
                    {"id": "string", "title": "string", "body": "string",
                     "tags": ["string"], "publish_time": "ISO8601", "url": "string"}
                ]
            },
            "note": "已检测 CLS_TELEGRAM_TOKEN，但 v1 未实装财联社接口。"
                    "v2 对接时在本脚本内实现 HTTP 调用并填充 items[]；"
                    "调用侧契约保持不变。",
        },
        summary="财联社电报：token 已设但 v1 接口未实装，返回空 items。",
    )


if __name__ == "__main__":
    main(sys.argv[1:])
