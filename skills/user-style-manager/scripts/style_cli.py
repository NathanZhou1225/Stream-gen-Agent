#!/usr/bin/env python3
"""CLI: user style memory — init, extract, import, list, get-context."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# scripts/ on sys.path
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import style_store
from paths import get_db_path, get_user_data_dir, get_user_id, get_workspace_root
from style_extract import extract_from_text, refine_from_text


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def cmd_init(_: argparse.Namespace) -> None:
    p = style_store.init_db()
    _emit(
        {
            "ok": True,
            "db_path": str(p),
            "user_data_dir": str(get_user_data_dir()),
            "workspace_root": str(get_workspace_root()),
        }
    )


def cmd_list(args: argparse.Namespace) -> None:
    uid = args.user_id or get_user_id()
    rows = style_store.list_styles(uid)
    out = [
        {
            "style_id": r.style_id,
            "style_name": r.style_name,
            "tags": r.tags,
            "created_at": r.created_at,
            "refine_count": r.refine_count,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]
    _emit({"ok": True, "user_id": uid, "count": len(out), "styles": out})


def cmd_import(args: argparse.Namespace) -> None:
    uid = args.user_id or get_user_id()
    raw = _read_text_file(Path(args.json_file))
    data = json.loads(raw)
    if not isinstance(data, dict):
        _emit({"ok": False, "error": "import 文件根须为 JSON object"})
        return
    style_name = data.get("style_name") or data.get("name")
    if not style_name:
        _emit({"ok": False, "error": "缺少 style_name"})
        return
    if isinstance(data.get("style_profile"), dict):
        profile = dict(data["style_profile"])
    else:
        profile = {
            k: data[k]
            for k in (
                "tone",
                "vocabulary_level",
                "sentence_structure",
                "catchphrases",
                "call_to_action",
            )
            if k in data
        }
    if "style_name" in profile:
        del profile["style_name"]  # type: ignore[dict-item]
    profile.pop("reference_texts", None)
    ref = data.get("reference_texts")
    if not isinstance(ref, list) or not ref:
        _emit({"ok": False, "error": "reference_texts 须为非空数组"})
        return
    tags = data.get("tags")
    if tags is not None and not isinstance(tags, list):
        _emit({"ok": False, "error": "tags 须为数组"})
        return
    note = data.get("source_note")
    # merge name into profile for downstream get_context
    prof_out: dict[str, Any] = dict(profile) if isinstance(profile, dict) else {}
    prof_out["style_name"] = str(style_name)
    prof_out.pop("reference_texts", None)
    sid = style_store.insert_style(
        user_id=uid,
        style_name=str(style_name),
        style_profile=prof_out,
        reference_texts=[str(x) for x in ref],
        tags=[str(t) for t in tags] if tags else None,
        source_note=str(note) if note else None,
        style_id=data.get("style_id"),
    )
    _emit({"ok": True, "style_id": sid, "user_id": uid})


def cmd_extract(args: argparse.Namespace) -> None:
    uid = args.user_id or get_user_id()
    if args.text_file:
        text = _read_text_file(Path(args.text_file))
    else:
        text = sys.stdin.read()
    if len(text.strip()) < 10:
        _emit({"ok": False, "error": "输入文本过短", "code": "EMPTY_INPUT"})
        return
    try:
        ex = extract_from_text(text)
    except Exception as e:  # noqa: BLE001
        _emit(
            {
                "ok": False,
                "error": str(e),
                "code": "EXTRACT_FAILED",
            }
        )
        return
    prof = ex["profile"]
    name_override = (args.style_name or "").strip()
    style_name = name_override or prof["style_name"]
    prof["style_name"] = style_name
    refs = prof.get("reference_texts") or []
    prof_for_db = {k: v for k, v in prof.items() if k != "reference_texts"}
    tags = []
    if args.tags:
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    sid = style_store.insert_style(
        user_id=uid,
        style_name=style_name,
        style_profile=prof_for_db,
        reference_texts=refs,
        tags=tags or None,
        source_note=args.source_note,
    )
    out = {
        "ok": True,
        "style_id": sid,
        "user_id": uid,
        "profile": prof,
        "reference_texts": refs,
    }
    if args.include_raw:
        out["raw_model"] = ex.get("raw_model")
    _emit(out)


def _build_context_block(row: style_store.StyleRow) -> str:
    p = row.style_profile
    lines = [
        "## User style (RAG block)",
        f"- **style_id**: `{row.style_id}`",
        f"- **style_name**: {row.style_name}",
    ]
    if row.tags:
        lines.append(f"- **tags**: {', '.join(row.tags)}")
    lines.append("")
    lines.append("### Profile (machine fields)")
    lines.append(json.dumps(p, ensure_ascii=False, indent=2))
    lines.append("")
    lines.append("### Few-shot reference excerpts (do not paraphrase for facts)")
    for i, chunk in enumerate(row.reference_texts, 1):
        lines.append(f"#### Ref {i}")
        lines.append(chunk.strip())
        lines.append("")
    lines.append(
        "---\n在生成口播/大纲/逐字稿时，**语气和句式**应贴近以上画像与引用；"
        "**事实与数据**仍以当前 topic 的 source_context / evidence 为准，不得用风格块覆盖事实。"
    )
    return "\n".join(lines).strip()


def cmd_get_context(args: argparse.Namespace) -> None:
    uid = args.user_id or get_user_id()
    style_store.init_db()
    row = style_store.get_style_for_user(uid, args.style_id)
    if row is None:
        _emit(
            {
                "ok": False,
                "error": "style 不存在或无权访问",
                "code": "STYLE_NOT_FOUND",
                "user_id": uid,
                "style_id": args.style_id,
            }
        )
        return
    block = _build_context_block(row)
    if args.format == "json":
        _emit(
            {
                "ok": True,
                "style_id": row.style_id,
                "user_id": uid,
                "style_name": row.style_name,
                "context_markdown": block,
            }
        )
    else:
        sys.stdout.write(block + "\n")


def cmd_delete(args: argparse.Namespace) -> None:
    uid = args.user_id or get_user_id()
    ok = style_store.delete_style(uid, args.style_id)
    _emit({"ok": ok, "user_id": uid, "style_id": args.style_id})


def cmd_refine(args: argparse.Namespace) -> None:
    """在既有 style 上，用新样本文稿经 LLM 合并画像（同 style_id，refine_count+1）。"""
    from datetime import datetime, timezone

    uid = args.user_id or get_user_id()
    style_store.init_db()
    row = style_store.get_style_for_user(uid, args.style_id)
    if row is None:
        _emit(
            {
                "ok": False,
                "error": "style 不存在或无权访问",
                "code": "STYLE_NOT_FOUND",
            }
        )
        return
    if args.text_file:
        text = _read_text_file(Path(args.text_file))
    else:
        text = sys.stdin.read()
    if len(text.strip()) < 10:
        _emit({"ok": False, "error": "新样本文过短", "code": "EMPTY_INPUT"})
        return
    try:
        ex = refine_from_text(
            row.style_name,
            row.style_profile,
            row.reference_texts,
            text,
        )
    except Exception as e:  # noqa: BLE001
        _emit({"ok": False, "error": str(e), "code": "REFINE_FAILED"})
        return
    prof = ex["profile"]
    refs = prof.get("reference_texts") or []
    style_name = (args.style_name or "").strip() or prof.get("style_name") or row.style_name
    prof_for_db = {k: v for k, v in prof.items() if k != "reference_texts"}
    prof_for_db["style_name"] = style_name
    note = row.source_note or ""
    if args.source_note:
        note = (note + "\n" + str(args.source_note).strip()).strip()[:2000]
    if not args.no_audit_note:
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        note = ((note + "\n" if note else "") + f"refine@{ts}").strip()[:2000]
    ok = style_store.update_style_row(
        uid,
        args.style_id,
        style_name=style_name,
        style_profile=prof_for_db,
        reference_texts=refs,
        tags=row.tags,
        source_note=note,
        increment_refine=True,
    )
    if not ok:
        _emit({"ok": False, "error": "update 失败", "code": "UPDATE_FAILED"})
        return
    out: dict[str, Any] = {
        "ok": True,
        "style_id": args.style_id,
        "user_id": uid,
        "refine_count": row.refine_count + 1,
        "style_name": style_name,
        "profile": prof,
        "reference_texts": refs,
    }
    if args.include_raw:
        out["raw_model"] = ex.get("raw_model")
    _emit(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="style_cli", description="user-style-manager")
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="创建 user_data 与 SQLite 表")
    p_init.set_defaults(func=cmd_init)

    p_list = sub.add_parser("list", help="列出某用户的风格")
    p_list.add_argument("--user-id", default=None, help="覆盖 OPENCLAW_USER_ID，默认 default")
    p_list.set_defaults(func=cmd_list)

    p_imp = sub.add_parser("import", help="从 JSON 文件导入一条（手修后入库）")
    p_imp.add_argument("--json-file", required=True)
    p_imp.add_argument("--user-id", default=None)
    p_imp.set_defaults(func=cmd_import)

    p_ex = sub.add_parser("extract", help="对长文本调用 LLM 提取风格并入库")
    p_ex.add_argument("--text-file", default=None, help="否则读 stdin")
    p_ex.add_argument("--user-id", default=None)
    p_ex.add_argument("--style-name", default=None, help="覆盖模型给出的 style_name")
    p_ex.add_argument("--tags", default=None, help="逗号分隔")
    p_ex.add_argument("--source-note", default=None)
    p_ex.add_argument(
        "--include-raw", action="store_true", help="在 JSON 输出附 raw 模型片段"
    )
    p_ex.set_defaults(func=cmd_extract)

    p_rf = sub.add_parser(
        "refine",
        help="在既有 style 上用新样本文再合并画像（同 UUID，不新建；需 Ark，见 prompts/refine-style.md）",
    )
    p_rf.add_argument("--style-id", required=True)
    p_rf.add_argument("--text-file", default=None, help="否则读 stdin")
    p_rf.add_argument("--user-id", default=None)
    p_rf.add_argument("--style-name", default=None, help="覆盖合并结果中的 style_name")
    p_rf.add_argument("--source-note", default=None, help="附加写入 source_note")
    p_rf.add_argument(
        "--no-audit-note",
        action="store_true",
        help="不自动在 source_note 追 refine@时间戳",
    )
    p_rf.add_argument(
        "--include-raw", action="store_true", help="在 JSON 输出附 raw 模型片段"
    )
    p_rf.set_defaults(func=cmd_refine)

    p_gc = sub.add_parser("get-context", help="取 RAG 拼接块（stdout）")
    p_gc.add_argument("--style-id", required=True)
    p_gc.add_argument("--user-id", default=None)
    p_gc.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="text=纯 markdown；json=包装 ok+context_markdown",
    )
    p_gc.set_defaults(func=cmd_get_context)

    p_del = sub.add_parser("delete", help="删除一条风格")
    p_del.add_argument("--style-id", required=True)
    p_del.add_argument("--user-id", default=None)
    p_del.set_defaults(func=cmd_delete)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
