#!/usr/bin/env python3
"""Run finance-source-ingest and print a single JSON snapshot (stdout).

不再拼接 Tavily 或其它联网兜底；`markdown_summary` 与 `meta` 以 ingest 输出为准。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SKILL_ROOT.parent.parent
FINANCE_ROOT = SKILL_ROOT.parent / "finance-source-ingest"


def _load_dotenv(env: dict[str, str]) -> dict[str, str]:
    for candidate in (
        WORKSPACE_ROOT / ".env",
        WORKSPACE_ROOT.parent / ".env",
        Path("/root/.openclaw/.env"),
    ):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key and key not in env:
                env[key] = value
    return env


def _extract_json_object(raw: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    last_obj: dict[str, Any] | None = None
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, end = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            tail = raw[idx + end :].strip()
            if not tail or tail.startswith("\n"):
                last_obj = obj
    if last_obj is None:
        raise ValueError("finance-source-ingest stdout did not contain a JSON object")
    return last_obj


def _run_finance_ingest(args: argparse.Namespace) -> dict[str, Any]:
    python_bin = FINANCE_ROOT / ".venv" / "bin" / "python"
    cmd = [
        str(python_bin if python_bin.exists() else sys.executable),
        str(FINANCE_ROOT / "scripts" / "ingest.py"),
        "run",
        "--sources",
        args.sources,
        "--max-items",
        str(args.max_items),
    ]
    if args.keywords:
        cmd.extend(["--keywords", args.keywords])

    env = _load_dotenv(dict(os.environ))
    proc = subprocess.run(
        cmd,
        cwd=str(WORKSPACE_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=args.timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"finance-source-ingest failed ({proc.returncode}): {proc.stdout[-2000:]}")
    return _extract_json_object(proc.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run market facts (finance-source-ingest JSON on stdout)")
    parser.add_argument("--sources", default="market,news,social")
    parser.add_argument("--keywords", default="")
    parser.add_argument("--max-items", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    snap = _run_finance_ingest(args)
    print(json.dumps(snap, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
