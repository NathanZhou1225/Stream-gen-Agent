#!/usr/bin/env python3
"""逐字稿轻量合规扫描。

规则：
- 词表来源：`../data/compliance/blacklist-common.txt`（或 --blacklist 指定）
- 每行一个词；以 # 开头或空行跳过；以 `re:` 开头的行按正则处理
- 字面量匹配 + 正则匹配，命中**只告警不改写**（用户自决）
- 扫描范围（三选一，互斥）：
    --from-draft <DID>   自动定位 active/{uid}/{DID}/script.json，遍历 segments[].say
    --script-file <path> 指定 script.json 文件
    --text <str>         直接扫一段文本（调试用）

输出：
    {
      "ok": true,
      "command": "lite_compliance_scan",
      "result": {
        "status": "pass" | "warn",
        "warnings_count": N,
        "warnings": [
          {"term": "必涨", "match_kind": "literal" | "regex",
           "segment_role": "cta", "at_time": "0:51-0:58",
           "context": "…这股必涨…", "offset": 12}
        ]
      }
    }

状态 `pass` = 0 命中；`warn` = ≥1 命中。
v1 设计目标：**命中也退出码 0**（是"告警"不是"错误"），由 Agent / 用户决定改不改。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from _common import (
    emit_error,
    emit_ok,
    get_active_draft_dir,
    get_user_id,
    now_iso,
    read_json,
    write_json_atomic,
)


SCANNER_VERSION = "v0.1.2"


# 黑词表默认位置：scripts/.. / data/compliance/blacklist-common.txt
DEFAULT_BLACKLIST = Path(__file__).resolve().parent.parent / "data" / "compliance" / "blacklist-common.txt"

CONTEXT_RADIUS = 12  # 命中处前后各展示多少字


# ---------- 黑词表加载 ----------

def load_blacklist(path: Path) -> tuple[list[str], list[re.Pattern[str]], list[str]]:
    """返回 (字面量列表, 正则列表, 加载到的原始模式字符串列表)。

    跳过空行 + # 注释。
    `re:` 前缀识别为正则。
    """
    if not path.exists():
        emit_error(
            "io",
            "BLACKLIST_NOT_FOUND",
            f"黑词表不存在：{path}",
            path=str(path),
        )

    literals: list[str] = []
    regexes: list[re.Pattern[str]] = []
    raw_patterns: list[str] = []

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        raw_patterns.append(s)
        if s.startswith("re:"):
            pat = s[3:].strip()
            try:
                regexes.append(re.compile(pat))
            except re.error as e:
                emit_error(
                    "config",
                    "BAD_REGEX",
                    f"黑词表 L{lineno} 正则无效：{pat} → {e}",
                    path=str(path),
                    lineno=lineno,
                )
        else:
            literals.append(s)

    return literals, regexes, raw_patterns


# ---------- 扫描单段文本 ----------

def scan_text(
    text: str,
    literals: list[str],
    regexes: list[re.Pattern[str]],
) -> list[dict[str, Any]]:
    """对一段文本做字面量 + 正则扫描，返回命中列表。

    每个命中含：term / match_kind / offset / context
    """
    hits: list[dict[str, Any]] = []
    for term in literals:
        start = 0
        while True:
            idx = text.find(term, start)
            if idx < 0:
                break
            hits.append({
                "term": term,
                "match_kind": "literal",
                "offset": idx,
                "context": _ctx(text, idx, idx + len(term)),
            })
            start = idx + len(term)

    for pat in regexes:
        for m in pat.finditer(text):
            hits.append({
                "term": m.group(0),
                "match_kind": "regex",
                "pattern": pat.pattern,
                "offset": m.start(),
                "context": _ctx(text, m.start(), m.end()),
            })
    return hits


def _ctx(text: str, start: int, end: int, r: int = CONTEXT_RADIUS) -> str:
    pre = text[max(0, start - r): start]
    hit = text[start:end]
    post = text[end: min(len(text), end + r)]
    return f"{pre}【{hit}】{post}"


# ---------- 输入路径：三种入口 ----------

def collect_segments_from_script_json(path: Path) -> list[dict[str, Any]]:
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        emit_error(
            "io",
            "SCRIPT_JSON_INVALID",
            f"script.json 不是 JSON object：{path}",
            path=str(path),
        )

    segments = data.get("segments") or []
    if not isinstance(segments, list):
        emit_error(
            "io",
            "SCRIPT_JSON_INVALID",
            "script.json 的 segments 字段必须是数组",
            path=str(path),
        )

    out = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        out.append({
            "index": i,
            "time": seg.get("time"),
            "role": seg.get("role"),
            "text": seg.get("say") or "",
        })
    return out


def locate_draft_script(draft_id: str) -> Path:
    user_id = get_user_id()
    draft_dir = get_active_draft_dir(user_id, draft_id)
    script_path = draft_dir / "script.json"
    if not script_path.exists():
        emit_error(
            "draft",
            "SCRIPT_NOT_FOUND",
            f"Draft #{draft_id} 下无 script.json（可能尚未进入 script_refining 阶段）",
            draft_id=draft_id,
            user_id=user_id,
            path=str(script_path),
        )
    return script_path


# ---------- --write-back：把扫描结果写回 script.json ----------

def write_back_compliance(
    script_path: Path,
    status: str,
    warnings: list[dict[str, Any]],
    *,
    draft_id: str | None,
) -> dict[str, Any]:
    """把 compliance 字段写回 script.json，并（当来源是 draft）刷 meta + 追加 history。

    返回 write-back 元信息，用于展示给调用方。
    """
    script_data = read_json(script_path, default=None)
    if not isinstance(script_data, dict):
        emit_error(
            "io",
            "SCRIPT_JSON_INVALID",
            f"无法写回 compliance：script.json 不是 JSON object → {script_path}",
            path=str(script_path),
        )

    ts = now_iso()
    script_data["compliance"] = {
        "status": status,
        "warnings": warnings,
        "scanned_at": ts,
        "scanner_version": SCANNER_VERSION,
    }
    write_json_atomic(script_path, script_data)

    info: dict[str, Any] = {
        "write_back_path": str(script_path),
        "scanned_at": ts,
        "scanner_version": SCANNER_VERSION,
    }

    if draft_id:
        user_id = get_user_id()
        draft_dir = get_active_draft_dir(user_id, draft_id)

        meta_path = draft_dir / "meta.json"
        meta = read_json(meta_path, default=None)
        if isinstance(meta, dict):
            meta["last_updated"] = ts
            write_json_atomic(meta_path, meta)
            info["meta_updated"] = True

        history_path = draft_dir / "history.json"
        history = read_json(history_path, default=[])
        if not isinstance(history, list):
            history = []
        history.append({
            "ts": ts,
            "action": "scan",
            "stage": meta.get("stage") if isinstance(meta, dict) else None,
            "note": f"合规扫描：{status}，命中 {len(warnings)} 处",
            "compliance_status": status,
            "warnings_count": len(warnings),
        })
        write_json_atomic(history_path, history)
        info["history_appended"] = True

    return info


# ---------- 主流程 ----------

def run(
    *,
    from_draft: str | None,
    script_file: str | None,
    text: str | None,
    blacklist_path: Path,
    write_back: bool = False,
) -> dict[str, Any]:
    literals, regexes, raw_patterns = load_blacklist(blacklist_path)

    warnings: list[dict[str, Any]] = []
    script_path: Path | None = None

    if text is not None:
        if write_back:
            emit_error(
                "usage",
                "WRITE_BACK_REQUIRES_DRAFT_OR_FILE",
                "--write-back 只能配合 --from-draft 或 --script-file 使用（--text 模式无落盘目标）。",
            )
        hits = scan_text(text, literals, regexes)
        for h in hits:
            warnings.append({
                **h,
                "segment_role": None,
                "at_time": None,
                "segment_index": None,
            })
        target_desc = f"text({len(text)} 字)"
    else:
        if from_draft:
            script_path = locate_draft_script(from_draft)
        else:
            script_path = Path(script_file)  # type: ignore[arg-type]
            if not script_path.exists():
                emit_error(
                    "io",
                    "SCRIPT_FILE_NOT_FOUND",
                    f"script-file 不存在：{script_path}",
                    path=str(script_path),
                )
        segments = collect_segments_from_script_json(script_path)
        for seg in segments:
            for h in scan_text(seg["text"], literals, regexes):
                warnings.append({
                    **h,
                    "segment_role": seg["role"],
                    "at_time": seg["time"],
                    "segment_index": seg["index"],
                })
        target_desc = f"script.json ({len(segments)} segments)"

    status = "warn" if warnings else "pass"

    result: dict[str, Any] = {
        "status": status,
        "target": target_desc,
        "blacklist": {
            "path": str(blacklist_path),
            "total_patterns": len(raw_patterns),
            "literal_count": len(literals),
            "regex_count": len(regexes),
        },
        "warnings_count": len(warnings),
        "warnings": warnings,
    }

    if write_back and script_path is not None:
        wb_info = write_back_compliance(
            script_path,
            status=status,
            warnings=warnings,
            draft_id=from_draft,
        )
        result["write_back"] = wb_info

    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="逐字稿轻量合规扫描")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--from-draft", help="Draft ID，自动定位 active 目录下的 script.json")
    group.add_argument("--script-file", help="直接指定 script.json 路径")
    group.add_argument("--text", help="直接扫描一段文本（调试用）")

    parser.add_argument(
        "--blacklist",
        default=str(DEFAULT_BLACKLIST),
        help=f"黑词表路径，默认 {DEFAULT_BLACKLIST}",
    )
    parser.add_argument(
        "--write-back",
        action="store_true",
        help="把扫描结果写回 script.json 的 compliance 字段（仅 --from-draft/--script-file 可用）。"
             "--from-draft 模式下同步刷 meta.last_updated + 追加 history.json 一条 action=scan。",
    )
    parser.add_argument("--json", action="store_true", help="兼容性开关（默认 JSON 输出）")
    args = parser.parse_args(argv)

    result = run(
        from_draft=args.from_draft,
        script_file=args.script_file,
        text=args.text,
        blacklist_path=Path(args.blacklist),
        write_back=args.write_back,
    )

    summary = (
        f"合规扫描：{result['status'].upper()}，命中 {result['warnings_count']} 处"
        f"（扫 {result['target']}，词表 {result['blacklist']['total_patterns']} 条）"
    )
    if result.get("write_back"):
        summary += "；结果已写回 script.json.compliance"

    emit_ok(
        command="lite_compliance_scan",
        result=result,
        summary=summary,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
