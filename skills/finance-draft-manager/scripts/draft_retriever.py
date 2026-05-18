#!/usr/bin/env python3
"""
finance-draft-manager — 本地 SQLite 检索已移除。

开稿证据包已与 preflight_topic --direction 合并（一次调用产出 topic_payload + candidate_evidence_packs），
无需二次 --candidate-id 调用；
全量讯息请使用 query_market_facts.py（云端 API）或 --live-fetch。
"""
from __future__ import annotations

import argparse
import json
import sys


def _removed(cmd: str) -> None:
    print(
        json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "LOCAL_SQLITE_REMOVED",
                    "message": f"draft_retriever {cmd} 已废弃（本地 finance_sources.db 已移除）",
                    "hint": "证据包：preflight_topic --direction → draft_manager --apply-topic-choice N；"
                    "拉数：query_market_facts.py 或 --live-fetch",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    sys.exit(2)


def main() -> None:
    if len(sys.argv) > 1:
        _removed(sys.argv[1])
    _removed("help")


if __name__ == "__main__":
    main()
