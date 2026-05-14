#!/usr/bin/env python3
"""
Sprint B5 独立入口：组装开稿用的 source_context + evidence_pack。

实现与契约见同目录 `draft_retriever.py` 的 `build-context` 子命令；
本文件仅为运维/文档中的固定路径别名，避免 Agent 只认文件名时漏调。
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    rest = sys.argv[1:]
    sys.argv = [str(here / "draft_retriever.py"), "build-context", *rest]
    import draft_retriever  # noqa: PLC0415 — 需在改写 argv 之后

    draft_retriever.main()


if __name__ == "__main__":
    main()
