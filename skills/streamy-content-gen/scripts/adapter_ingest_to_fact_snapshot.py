#!/usr/bin/env python3
"""finance-source-ingest 快照 (stdin) → FactSnapshot v1.0 (stdout)。

路线 A：本脚本输出为跨 skill 的稳定契约；编排层 / Agent 将 facts
折叠进 topic_picking 的 `source_context` 与各 `candidates[].evidence_anchor`，
draft_manager 不读本格式，不解析原始 ingest JSON。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 与 _meta / ingest pipeline 产品版本对齐
PROV_PREFIX = "ingest:finance-source-ingest:v0.1"
SCHEMA = "1.0"


def _pack(line: str, prov_suffix: str) -> dict[str, str]:
    full = f"{PROV_PREFIX}:{prov_suffix}"
    return {
        "evidence_anchor": f"{line} | provenance: {full}",
        "provenance": full,
    }


def _facts_from_snapshot(
    snap: dict[str, Any], *, cap: int
) -> tuple[list[str], list[dict[str, str]]]:
    sc: set[str] = set()
    facts: list[dict[str, str]] = []
    sec = snap.get("sections") or {}
    m = sec.get("market") or {}
    n = sec.get("news") or {}
    s = sec.get("social") or {}

    def push_sc(tag: str) -> None:
        sc.add(f"{PROV_PREFIX}:{tag}")

    def add(line: str, p: str) -> bool:
        nonlocal facts
        if len(facts) >= cap:
            return False
        facts.append(_pack(line, p))
        return True

    aidx = (m.get("a_share_indices") or {}).get("items") or []
    if aidx:
        push_sc("market")
    for it in aidx:
        if len(facts) >= cap:
            break
        nm, cl, pc = (it.get("name"), it.get("close"), it.get("pct_change"))
        if pc is not None and isinstance(pc, (int, float)) and cl is not None:
            add(f"{nm} 收{cl} ({pc:+.2f}%)", "market:a_share")
        elif cl is not None:
            add(f"{nm} 收{cl}", "market:a_share")

    nb = m.get("northbound") or {}
    y = nb.get("aggregate_net_buy_yi")
    if y is not None and len(facts) < cap:
        push_sc("market")
        add(f"北向净买入额(汇总) {y} 亿元", "market:northbound")

    ir = (m.get("industry_rank") or {}).get("items") or []
    for it in ir:
        if len(facts) >= cap:
            break
        name, pc = it.get("name"), it.get("pct_change")
        if name and pc is not None and isinstance(pc, (int, float)):
            push_sc("market")
            add(f"行业资金流 Top：{name} 今日涨跌幅 {pc:+.2f}%", "market:industry")
        elif name:
            push_sc("market")
            if not add(str(name), "market:industry"):
                break

    ost = m.get("overseas_stub") or {}
    us = (ost.get("overseas") or {}).get("us_indices") or []
    for u in us:
        if len(facts) >= cap:
            break
        nm, pc = u.get("name"), u.get("pct_change")
        if nm and isinstance(pc, (int, float)):
            push_sc("market")
            if not add(f"海外 {nm} 涨跌幅 {pc:+.2f}%", "market:overseas"):
                break

    nitems = n.get("items") or []
    if nitems:
        push_sc("news")
    for it in nitems:
        if len(facts) >= cap:
            break
        t, c = (it.get("title") or ""), (it.get("clean_text") or "")
        one = t if len(t) >= len(c) else c
        one = (one or t or c).replace("\n", " ").strip()[:200]
        if one:
            add(f"快讯: {one}", "news:cls_telegraph")

    sitems = s.get("items") or []
    if sitems:
        push_sc("social")
    for it in sitems:
        if len(facts) >= cap:
            break
        t, c = (it.get("title") or ""), (it.get("clean_text") or it.get("hot") or "")
        pl = (it.get("platform") or "").strip()
        one = f"[{pl}] {t} {c}".replace("\n", " ").strip()[:200] if pl else f"{t} {c}".strip()[:200]
        if one:
            add(one, "social:api")

    return sorted(sc), facts


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ingest 快照 → FactSnapshot v1.0 JSON（stdout）"
    )
    ap.add_argument(
        "-f",
        "--in-file",
        default="",
        help="读文件而非 stdin；默认可从 ingest: python ingest.py run ... 管道传入",
    )
    ap.add_argument(
        "--max-facts", type=int, default=32, help="单条 snapshot 最大事实条数"
    )
    args = ap.parse_args()
    raw = Path(args.in_file).read_text(encoding="utf-8") if args.in_file else sys.stdin.read()
    snap: dict[str, Any] = json.loads(raw) if raw.strip() else {}
    if not isinstance(snap, dict):
        print("{}", file=sys.stderr)
        return 2
    up_meta = snap.get("meta")
    if not isinstance(up_meta, dict):
        up_meta = {}
    sctx, facts = _facts_from_snapshot(snap, cap=max(1, min(args.max_facts, 200)))
    out: dict[str, Any] = {
        "schema_version": SCHEMA,
        "source_context": sctx,
        "facts": facts,
        "meta": {
            "contract": "streamy:FactSnapshot",
            "upstream_ingest_schema": snap.get("schema_version", ""),
            "fetched_at": up_meta.get("fetched_at", ""),
        },
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
