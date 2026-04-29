#!/usr/bin/env python3
"""Run finance-source-ingest and append deterministic Tavily fallback supplements."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SKILL_ROOT.parent.parent
FINANCE_ROOT = SKILL_ROOT.parent / "finance-source-ingest"
TAVILY_ROOT = SKILL_ROOT.parent / "liang-tavily-search-1.0.1"
CST = timezone(timedelta(hours=8))


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

    proc = subprocess.run(
        cmd,
        cwd=str(WORKSPACE_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=args.timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"finance-source-ingest failed ({proc.returncode}): {proc.stdout[-2000:]}")
    return _extract_json_object(proc.stdout)


def _query_for_gap(area: str, reason: str, today: str) -> str:
    text = f"{area} {reason}"
    if "北向" in text:
        return f"{today} 北向资金 沪深港通 净流入 A股"
    if "社媒" in text or "人气榜" in text or "舆情" in text:
        return f"{today} A股 人气榜 热门股票 社媒 舆情"
    if "泛财经" in text or "热点" in text:
        return f"{today} 今日财经热点 A股 港股 宏观"
    if "大事件" in text or "国家" in text or "全球" in text:
        return f"近7天 全球宏观 政策 地缘 央行 关税 金融市场 重要事件"
    return f"{today} {area} A股 金融市场"


def _planned_queries(gaps: list[dict[str, str]], today: str) -> list[tuple[str, str]]:
    priority = ("北向资金", "社媒/人气榜/舆情", "人气榜", "泛财经热点", "国家/全球大事件")
    ordered = sorted(gaps, key=lambda g: priority.index(g.get("area", "")) if g.get("area", "") in priority else 99)
    seen_queries: set[str] = set()
    result: list[tuple[str, str]] = []
    for gap in ordered:
        area = str(gap.get("area") or "联网缺口")
        reason = str(gap.get("reason") or "")
        query = _query_for_gap(area, reason, today)
        if query in seen_queries:
            continue
        seen_queries.add(query)
        result.append((area, query))
        if len(result) >= 4:
            break
    return result


def _run_tavily(area: str, query: str, env: dict[str, str]) -> dict[str, Any]:
    script = TAVILY_ROOT / "scripts" / "search.mjs"
    if not script.exists():
        return {"area": area, "query": query, "ok": False, "error": f"Tavily script not found: {script}"}
    if not env.get("TAVILY_API_KEY"):
        return {"area": area, "query": query, "ok": False, "error": "TAVILY_API_KEY not set"}

    base_cmd = ["node", str(script), query, "-n", "3", "--json"]
    proc = subprocess.run(
        [*base_cmd, "--raw-content"],
        cwd=str(WORKSPACE_ROOT),
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        env=env,
        check=False,
    )
    stdout_text = proc.stdout.decode("utf-8", errors="ignore")
    stderr_text = proc.stderr.decode("utf-8", errors="ignore")
    if proc.returncode != 0:
        return {"area": area, "query": query, "ok": False, "error": (stderr_text or stdout_text)[-800:]}
    try:
        data = json.loads(stdout_text)
    except json.JSONDecodeError:
        retry = subprocess.run(
            base_cmd,
            cwd=str(WORKSPACE_ROOT),
            text=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env=env,
            check=False,
        )
        retry_stdout = retry.stdout.decode("utf-8", errors="ignore")
        retry_stderr = retry.stderr.decode("utf-8", errors="ignore")
        if retry.returncode != 0:
            return {"area": area, "query": query, "ok": False, "error": (retry_stderr or retry_stdout)[-800:]}
        try:
            data = json.loads(retry_stdout)
        except json.JSONDecodeError as exc2:
            return {"area": area, "query": query, "ok": False, "error": f"Tavily JSON parse failed: {exc2}"}
    return {"area": area, "query": query, "ok": True, "data": data}


def _format_tavily_section(results: list[dict[str, Any]], gaps: list[dict[str, str]]) -> str:
    if not results:
        return ""

    gap_text = "、".join(str(g.get("area") or "") for g in gaps[:4] if g.get("area"))
    lines = [
        "",
        "### 🔍 联网补充（Tavily 兜底）",
        f"> 触发原因：{gap_text or '部分 API 缺口'}；以下为独立联网补充，不覆盖上方 API 数字。",
    ]
    fetched_at = datetime.now(CST).isoformat(timespec="seconds")
    for item in results:
        area = item.get("area") or "联网补充"
        query = item.get("query") or ""
        lines.append(f"- **{area}**（检索时间：{fetched_at}；查询：{query}）")
        if not item.get("ok"):
            lines.append(f"  - 未执行成功：{item.get('error') or 'unknown error'}")
            continue
        data = item.get("data") or {}
        rows = (data.get("results") or [])[:2]
        if not rows:
            lines.append("  - 未找到可核验补充。")
            continue
        for row in rows:
            title = str(row.get("title") or "").strip()
            url = str(row.get("url") or "").strip()
            content = str(row.get("content") or "").strip()
            raw_content = str(row.get("raw_content") or "").strip()
            snippet_src = content or raw_content
            snippet = snippet_src[:220] + ("…" if len(snippet_src) > 220 else "")
            if title and url:
                lines.append(f"  - {title}｜{url}")
            if snippet:
                lines.append(f"    摘要：{snippet}")
            elif title:
                lines.append(f"    摘要：该来源标题为“{title}”，原站未返回可截取正文。")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run market facts and append Tavily fallback supplements")
    parser.add_argument("--sources", default="market,news,social")
    parser.add_argument("--keywords", default="")
    parser.add_argument("--max-items", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    snap = _run_finance_ingest(args)
    meta = snap.setdefault("meta", {})
    gaps = [g for g in (meta.get("websearch_gaps") or []) if isinstance(g, dict)]
    existing_md = str(snap.get("markdown_summary") or "")
    if (meta.get("websearch_required") or gaps) and not meta.get("websearch_executed") and "联网补充（Tavily 兜底）" not in existing_md:
        today = datetime.now(CST).strftime("%Y年%m月%d日")
        env = _load_dotenv(dict(os.environ))
        supplements = [_run_tavily(area, query, env) for area, query in _planned_queries(gaps, today)]
        section = _format_tavily_section(supplements, gaps)
        if section:
            snap["markdown_summary"] = str(snap.get("markdown_summary") or "").rstrip() + "\n\n" + section
        meta["websearch_executed"] = True
        meta["websearch_provider"] = "tavily-search"
        meta["websearch_supplements"] = supplements

    print(json.dumps(snap, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
