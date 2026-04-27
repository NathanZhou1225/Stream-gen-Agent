#!/usr/bin/env python3
"""FactSnapshot v1.0 (stdin) → `topic_picking` payload JSON (stdout).

确定性组装，供 `draft_manager update --stage topic_picking --payload-file` 使用。
title/angle 为可替换占位；`source_context` 与 `evidence_anchor` 为审计主责。"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from typing import Any

_PROV = re.compile(r"provenance:\s*(\S+)", re.I)
# 与 prompts/topic-generation.md §3 一致：3 条至少 2 种类型 → 三槽固定为三种不同类型
_TYPE_SLOTS = ("反直觉钩子", "数据派", "反问派")


def _drop_incomplete_trailing_parens(s: str) -> str:
    """截断后若 `(`/（ 多于 `）`/），从最后一个未闭合括号起去掉尾部，避免「少右括号」观感。"""
    t = s
    while t.count("(") > t.count(")"):
        j = t.rfind("(")
        if j < 0:
            break
        t = t[:j].rstrip()
    while t.count("（") > t.count("）"):
        j = t.rfind("（")
        if j < 0:
            break
        t = t[:j].rstrip()
    return t.rstrip(" ，,")


def _title_from_evidence(anchor: str) -> str:
    base = anchor.split("|", 1)[0].strip()
    base = re.sub(r"^(快讯:)\s*", "", base)
    if not base:
        return "基于事实的选题（见证据）"
    if len(base) > 30:
        base = base[:30]
    base = _drop_incomplete_trailing_parens(base)
    if len(base) > 30:
        base = base[:29] + "…"
    return base


def _select_facts(
    facts: list[dict[str, Any]], n: int, pick: str, seed: int | None
) -> list[dict[str, Any]]:
    if len(facts) < n:
        raise ValueError(f"需要至少 {n} 条 facts，当前 {len(facts)}")
    if pick == "first":
        return facts[:n]
    rng = random.Random(seed)
    idx = list(range(len(facts)))
    rng.shuffle(idx)
    return [facts[i] for i in idx[:n]]


def _build(
    snap: dict[str, Any], selected: list[dict[str, Any]], topic: str
) -> dict[str, Any]:
    sctx: list[str] = list(snap.get("source_context") or [])
    if not sctx:
        raise ValueError("source_context 不能为空")
    candidates: list[dict[str, Any]] = []
    for i, f in enumerate(selected, start=1):
        ev = f.get("evidence_anchor")
        if not isinstance(ev, str) or not ev.strip():
            ev = str(f.get("provenance") or "")
        prov = (f.get("provenance") or "").strip()
        m = _PROV.search(ev)
        if m:
            prov = m.group(1).strip()
        ctype = _TYPE_SLOTS[min(i - 1, len(_TYPE_SLOTS) - 1)]
        if i > len(_TYPE_SLOTS):
            ctype = "数据派"
        title = _title_from_evidence(ev)
        if i == 3 and not (title.endswith("?") or title.endswith("？")):
            # 在较短前缀上加「？」，并去掉可能半截的 (…，避免截断掉右括号
            t2 = _drop_incomplete_trailing_parens(title[:26])
            cand = f"{t2}？" if t2 else title
            if len(cand) > 30:
                t2b = _drop_incomplete_trailing_parens(title[:20])
                cand = f"{t2b}？" if t2b else title
            if len(cand) <= 30 and cand:
                title = cand
        seg = prov.split(":")[-1] if ":" in prov else prov
        candidates.append(
            {
                "id": i,
                "type": ctype,
                "title": title,
                "angle_summary": f"以可核对事实（{seg}）为锚，类型「{ctype}」仅为叙事包装，不替代证据句。",
                "evidence_anchor": ev.strip(),
                "risk_flag": None,
            }
        )
    t0 = topic or _title_from_evidence(selected[0].get("evidence_anchor") or "")
    if not t0 or t0 == "基于事实的选题（见证据）":
        t0 = "市场与资讯：事实锚点见下"
    return {
        "candidates": candidates,
        "chosen": None,
        "topic": t0,
        "source_context": sctx,
        "notes_for_next_stage": (
            "本 payload 由 `build_topic_payload.py` 从 FactSnapshot 确定性生成；"
            "可再用 topic-generation 润色 title/angle，保留 evidence_anchor 中 `| provenance:` 整段。"
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="FactSnapshot → topic_picking payload (stdout JSON)")
    ap.add_argument("-f", "--in-file", default="", help="输入文件，默认 stdin")
    ap.add_argument("--topic", default="", help="payload.topic")
    ap.add_argument("--candidates", type=int, default=3, help="候选条数，默认 3")
    ap.add_argument(
        "--pick",
        choices=("first", "random"),
        default="first",
        help="取 facts 子集：顺序前 N 或随机 N 条",
    )
    ap.add_argument("--seed", type=int, default=42, help="--pick random 时的种子（默认可复现）")
    args = ap.parse_args()
    raw = (open(args.in_file, encoding="utf-8") if args.in_file else sys.stdin).read()
    if not raw.strip():
        print("empty input", file=sys.stderr)
        return 2
    snap: dict[str, Any] = json.loads(raw)
    facts: list[dict[str, Any]] = list(snap.get("facts") or [])
    n = max(1, min(args.candidates, 10))
    try:
        selected = _select_facts(facts, n, args.pick, args.seed)
        payload = _build(snap, selected, args.topic)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
