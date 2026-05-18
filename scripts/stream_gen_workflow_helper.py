#!/usr/bin/env python3
"""Safe workflow helper for stream-gen.

Bundles only non-decision steps. It deliberately stops at user choice gates:
- start-topic: create draft -> preflight -> topic_picking update
- apply-choice: apply-topic-choice（内嵌证据包，1 次 draft_manager）或 legacy preflight 回退
- list-styles: style_cli list --with-context（飞书展示选型）
- bind-style: set-style-id + get-context 一步返回 user_style_context
- prevalidate-script: 生成前 schema 预检（附录≤5、evidence_source_type 白名单等）
- validate-script: wrapper around draft_manager update --validate-only
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
STREAMY_SCRIPTS = WORKSPACE_ROOT / "skills" / "streamy-content-gen" / "scripts"
DRAFT_MANAGER = STREAMY_SCRIPTS / "draft_manager.py"
PREFLIGHT_TOPIC = STREAMY_SCRIPTS / "preflight_topic.py"
STYLE_CLI = WORKSPACE_ROOT / "skills" / "user-style-manager" / "scripts" / "style_cli.py"
DEFAULT_SNAPSHOT_CACHE = WORKSPACE_ROOT / "cache" / "snapshot" / "snapshot.json"
TOPIC_CANDIDATES_JSON = "topic_candidates.json"
RUN_ROOT = Path("/tmp/stream_gen_workflow")

if str(STREAMY_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(STREAMY_SCRIPTS))

from _common import get_active_draft_dir, get_user_id, read_json  # noqa: E402
from preflight_topic import validate_candidate_choice  # noqa: E402


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fail(code: str, message: str, **extra: Any) -> None:
    payload = {"ok": False, "error_code": code, "message": message}
    payload.update(extra)
    _emit(payload)
    raise SystemExit(1)


def _run_json_soft(cmd: list[str], *, cwd: Path = WORKSPACE_ROOT, timeout: int = 180) -> tuple[int, dict[str, Any]]:
    """与 _run_json 相同，但不因 returncode!=0 抛错，供 embedded/fallback 分支判断。"""
    cp = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        data = json.loads(cp.stdout) if (cp.stdout or "").strip() else {}
    except json.JSONDecodeError as e:
        _fail(
            "INVALID_JSON_OUTPUT",
            f"命令未返回合法 JSON：{e}",
            command=cmd,
            stdout=(cp.stdout or "")[:2000],
            stderr=(cp.stderr or "")[:2000],
        )
    if not isinstance(data, dict):
        _fail("INVALID_JSON_TYPE", "命令 JSON 输出不是 object。", command=cmd)
    return cp.returncode, data


def _run_json(cmd: list[str], *, cwd: Path = WORKSPACE_ROOT, timeout: int = 180) -> dict[str, Any]:
    cp = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if cp.returncode != 0:
        _fail(
            "COMMAND_FAILED",
            f"命令失败：{' '.join(cmd)}",
            returncode=cp.returncode,
            stdout=(cp.stdout or "")[-2000:],
            stderr=(cp.stderr or "")[-2000:],
        )
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        _fail(
            "INVALID_JSON_OUTPUT",
            f"命令未返回合法 JSON：{e}",
            command=cmd,
            stdout=(cp.stdout or "")[:2000],
            stderr=(cp.stderr or "")[:2000],
        )
    if not isinstance(data, dict):
        _fail("INVALID_JSON_TYPE", "命令 JSON 输出不是 object。", command=cmd)
    return data


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def _run_dir(draft_id: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return RUN_ROOT / f"{stamp}-{draft_id}"


def _extract_draft_id(create_result: dict[str, Any]) -> str:
    result = create_result.get("result")
    if isinstance(result, dict) and isinstance(result.get("draft_id"), str):
        return result["draft_id"]
    _fail("DRAFT_ID_MISSING", "draft_manager create 返回中缺少 result.draft_id。", result=create_result)
    return ""


def _draft_dir(draft_id: str) -> Path:
    d = get_active_draft_dir(get_user_id(), draft_id)
    if not d.is_dir():
        _fail("DRAFT_NOT_FOUND", f"未找到 active Draft #{draft_id}。", draft_id=draft_id, path=str(d))
    return d


def _load_topic_payload_from_draft(draft_id: str) -> tuple[dict[str, Any], Path]:
    topic_path = _draft_dir(draft_id) / TOPIC_CANDIDATES_JSON
    payload = read_json(topic_path, default=None)
    if not isinstance(payload, dict):
        _fail(
            "TOPIC_PAYLOAD_MISSING",
            f"Draft #{draft_id} 缺少 {TOPIC_CANDIDATES_JSON}，请先完成 start-topic 或 preflight 落盘。",
            draft_id=draft_id,
            path=str(topic_path),
        )
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        _fail(
            "TOPIC_CANDIDATES_EMPTY",
            f"Draft #{draft_id} 的 candidates 为空，无法生成 evidence_pack。",
            draft_id=draft_id,
        )
    return payload, topic_path


def _has_embedded_evidence_pack(topic_payload: dict[str, Any], candidate_id: int) -> bool:
    packs = topic_payload.get("candidate_evidence_packs")
    if not isinstance(packs, dict):
        return False
    pack = packs.get(str(candidate_id))
    if pack is None:
        pack = packs.get(candidate_id)  # type: ignore[arg-type]
    return isinstance(pack, dict) and bool(pack.get("candidate_title"))


def _resolve_snapshot_path(
    topic_payload: dict[str, Any],
    explicit: str | None,
) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            _fail("SNAPSHOT_NOT_FOUND", f"snapshot 文件不存在：{p}", snapshot_path=str(p))
        return p.resolve()
    meta = topic_payload.get("preflight_meta")
    if isinstance(meta, dict):
        cached = meta.get("snapshot_path")
        if cached:
            p = Path(str(cached))
            if p.is_file():
                return p.resolve()
    if DEFAULT_SNAPSHOT_CACHE.is_file():
        return DEFAULT_SNAPSHOT_CACHE.resolve()
    _fail(
        "SNAPSHOT_NOT_FOUND",
        "未找到可用 snapshot：请传 --snapshot-path 或先执行 query_market_facts / preflight 写入缓存。",
        default_cache=str(DEFAULT_SNAPSHOT_CACHE),
    )
    return DEFAULT_SNAPSHOT_CACHE


def _resolve_topic_payload_file(
    draft_id: str,
    explicit: str | None,
) -> tuple[dict[str, Any], Path]:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            _fail("TOPIC_PAYLOAD_NOT_FOUND", f"topic_payload 文件不存在：{p}", path=str(p))
        payload = read_json(p, default=None)
        if not isinstance(payload, dict):
            _fail("TOPIC_PAYLOAD_INVALID", f"topic_payload 必须是 JSON object：{p}", path=str(p))
        return payload, p.resolve()
    return _load_topic_payload_from_draft(draft_id)


def cmd_start_topic(args: argparse.Namespace) -> None:
    create_cmd = [sys.executable, str(DRAFT_MANAGER), "create", "--json"]
    if args.topic:
        create_cmd.extend(["--topic", args.topic])
    if args.style_id:
        create_cmd.extend(["--style-id", args.style_id])
    create = _run_json(create_cmd, timeout=60)
    draft_id = _extract_draft_id(create)

    preflight_cmd = [
        sys.executable,
        str(PREFLIGHT_TOPIC),
        "--direction",
        args.direction,
    ]
    if args.finance_root:
        preflight_cmd.extend(["--finance-root", args.finance_root])
    preflight = _run_json(preflight_cmd, timeout=args.timeout_sec)
    if not preflight.get("ok"):
        _fail("PREFLIGHT_FAILED", "preflight_topic.py 返回 ok=false。", draft_id=draft_id, preflight=preflight)

    topic_payload = preflight.get("topic_payload")
    if not isinstance(topic_payload, dict):
        _fail("TOPIC_PAYLOAD_MISSING", "preflight 输出缺少 topic_payload object。", draft_id=draft_id, preflight=preflight)
    snapshot_path = preflight.get("snapshot_path") or str(DEFAULT_SNAPSHOT_CACHE)

    topic_payload_path: str
    delete_after_update = False
    run_dir: str | None = None
    preflight_path: str | None = None
    if getattr(args, "keep_run_artifacts", False):
        run_dir_path = _run_dir(draft_id)
        run_dir = str(run_dir_path)
        topic_payload_path = str(run_dir_path / "topic_payload.json")
        preflight_path = str(run_dir_path / "preflight_output.json")
        _write_json(Path(topic_payload_path), topic_payload)
        _write_json(Path(preflight_path), preflight)
    else:
        fd, topic_payload_path = tempfile.mkstemp(suffix=f"-{draft_id}-topic.json", prefix="stream_gen_")
        os.close(fd)
        _write_json(Path(topic_payload_path), topic_payload)
        delete_after_update = True

    try:
        update = _run_json(
            [
                sys.executable,
                str(DRAFT_MANAGER),
                "update",
                "--draft",
                draft_id,
                "--stage",
                "topic_picking",
                "--payload-file",
                topic_payload_path,
                "--edit-note",
                "helper start-topic: preflight 后落盘三候选",
                "--json",
            ],
            timeout=60,
        )
    finally:
        if delete_after_update:
            Path(topic_payload_path).unlink(missing_ok=True)

    draft_topic_path = _draft_dir(draft_id) / TOPIC_CANDIDATES_JSON
    _emit(
        {
            "ok": True,
            "command": "start-topic",
            "result": {
                "draft_id": draft_id,
                "direction": args.direction,
                "run_dir": run_dir,
                "topic_candidates_file": str(draft_topic_path),
                "topic_payload_file": str(draft_topic_path),
                "preflight_output_file": preflight_path,
                "snapshot_path": snapshot_path,
                "snapshot_cached": preflight.get("snapshot_cached"),
                "has_candidate_evidence_packs": isinstance(topic_payload.get("candidate_evidence_packs"), dict),
                "topic_update": update.get("result"),
                "candidates": topic_payload.get("candidates", []),
                "relevance_scores": (topic_payload.get("preflight_meta") or {}).get("relevance_scores"),
                "feishu_digest_bullets": topic_payload.get("feishu_digest_bullets", []),
                "apply_choice_hint": (
                    f"python3 scripts/stream_gen_workflow_helper.py apply-choice "
                    f"--draft {draft_id} --candidate-id <1|2|3>"
                ),
            },
            "summary": f"已创建 Draft #{draft_id}，完成 preflight 并落盘 topic_picking；下一步等待用户选择 1/2/3。",
        }
    )


def _embedded_evidence_pack(topic_payload: dict[str, Any], candidate_id: int) -> dict[str, Any] | None:
    packs = topic_payload.get("candidate_evidence_packs")
    if not isinstance(packs, dict):
        return None
    pack = packs.get(str(candidate_id))
    if pack is None:
        pack = packs.get(candidate_id)  # type: ignore[arg-type]
    return pack if isinstance(pack, dict) else None


def _apply_choice_via_preflight(
    args: argparse.Namespace,
    topic_payload_path: Path,
    snapshot_path: Path,
    choice_check: dict[str, Any],
) -> None:
    """旧稿回退：set-chosen + preflight --candidate-id + set-evidence-pack-file（写临时包装 JSON）。"""
    set_chosen = _run_json(
        [
            sys.executable,
            str(DRAFT_MANAGER),
            "update",
            "--draft",
            args.draft,
            "--set-chosen",
            str(args.candidate_id),
            "--edit-note",
            f"helper apply-choice (legacy): 选择候选 {args.candidate_id}",
            "--json",
        ],
        timeout=60,
    )
    cmd = [
        sys.executable,
        str(PREFLIGHT_TOPIC),
        "--candidate-id",
        str(args.candidate_id),
        "--topic-payload-file",
        str(topic_payload_path),
        "--snapshot-path",
        str(snapshot_path),
    ]
    if args.allow_targeted_fetch:
        cmd.append("--allow-targeted-fetch")
    pack_result = _run_json(cmd, timeout=args.timeout_sec)
    if not pack_result.get("ok"):
        _fail("EVIDENCE_PACK_FAILED", "preflight evidence_pack 返回 ok=false。", draft_id=args.draft, preflight=pack_result)

    fd, pack_path_str = tempfile.mkstemp(suffix=f"-{args.draft}-evidence.json", prefix="stream_gen_")
    os.close(fd)
    pack_path = Path(pack_path_str)
    _write_json(pack_path, pack_result)
    try:
        persist = _run_json(
            [
                sys.executable,
                str(DRAFT_MANAGER),
                "update",
                "--draft",
                args.draft,
                "--set-evidence-pack-file",
                str(pack_path),
                "--edit-note",
                f"helper apply-choice (legacy): 落盘候选 {args.candidate_id} 证据包",
                "--json",
            ],
            timeout=60,
        )
    finally:
        pack_path.unlink(missing_ok=True)

    evidence_pack = pack_result.get("evidence_pack", pack_result)
    draft_pack = _draft_dir(args.draft) / "candidate_evidence_pack.json"
    _emit(
        {
            "ok": True,
            "command": "apply-choice",
            "result": {
                "draft_id": args.draft,
                "candidate_id": args.candidate_id,
                "path": "legacy_preflight",
                "topic_payload_file": str(topic_payload_path),
                "snapshot_path": str(snapshot_path),
                "evidence_pack_file": str(draft_pack),
                "set_chosen": set_chosen.get("result"),
                "persist": persist.get("result"),
                "choice_validation": choice_check,
                "evidence_pack": evidence_pack,
                "source_gaps": evidence_pack.get("source_gaps") if isinstance(evidence_pack, dict) else [],
            },
            "summary": f"Draft #{args.draft} 已通过 legacy preflight 落盘证据包；下一步展示证据包并等待 user-style。",
        }
    )


def cmd_apply_choice(args: argparse.Namespace) -> None:
    topic_payload, topic_payload_path = _resolve_topic_payload_file(args.draft, args.topic_payload_file)
    snapshot_path = _resolve_snapshot_path(topic_payload, args.snapshot_path)

    if not args.skip_relevance_check:
        try:
            choice_check = validate_candidate_choice(
                topic_payload,
                str(args.candidate_id),
                min_score=args.min_relevance,
            )
        except ValueError as e:
            _fail("CHOICE_VALIDATION_FAILED", str(e), draft_id=args.draft)
        if not choice_check.get("ok"):
            _fail(
                "CANDIDATE_LOW_RELEVANCE",
                choice_check.get("hint") or "所选候选与开稿方向关联度不足",
                draft_id=args.draft,
                choice_validation=choice_check,
            )
    else:
        choice_check = {"ok": True, "skipped": True}

    use_embedded = not args.force_preflight and _has_embedded_evidence_pack(topic_payload, args.candidate_id)
    if use_embedded:
        _rc, applied = _run_json_soft(
            [
                sys.executable,
                str(DRAFT_MANAGER),
                "update",
                "--draft",
                args.draft,
                "--apply-topic-choice",
                str(args.candidate_id),
                "--edit-note",
                f"helper apply-choice: 内嵌证据包选定候选 {args.candidate_id}",
                "--json",
            ],
            timeout=60,
        )
        if applied.get("ok"):
            evidence_pack = _embedded_evidence_pack(topic_payload, args.candidate_id) or read_json(
                Path(applied.get("result", {}).get("evidence_pack_path", "")),
                default={},
            )
            _emit(
                {
                    "ok": True,
                    "command": "apply-choice",
                    "result": {
                        "draft_id": args.draft,
                        "candidate_id": args.candidate_id,
                        "path": "embedded",
                        "topic_candidates_file": str(_draft_dir(args.draft) / TOPIC_CANDIDATES_JSON),
                        "evidence_pack_file": applied.get("result", {}).get("evidence_pack_path"),
                        "apply": applied.get("result"),
                        "choice_validation": choice_check,
                        "evidence_pack": evidence_pack,
                        "source_gaps": evidence_pack.get("source_gaps") if isinstance(evidence_pack, dict) else [],
                    },
                    "summary": f"Draft #{args.draft} 已选定候选 {args.candidate_id} 并落盘内嵌证据包（1 次 draft_manager，无 /tmp 证据 JSON）。",
                }
            )
            return
        if applied.get("error_code") != "EVIDENCE_PACK_NOT_PRECOMPUTED":
            _fail(
                "APPLY_TOPIC_CHOICE_FAILED",
                applied.get("message") or "apply-topic-choice 失败",
                draft_id=args.draft,
                draft_manager=applied,
            )

    _apply_choice_via_preflight(args, topic_payload_path, snapshot_path, choice_check)


def cmd_list_styles(args: argparse.Namespace) -> None:
    cmd = [sys.executable, str(STYLE_CLI), "list", "--json"]
    if args.with_context:
        cmd.append("--with-context")
    if args.max_context_chars:
        cmd.extend(["--max-context-chars", str(args.max_context_chars)])
    if args.user_id:
        cmd.extend(["--user-id", args.user_id])
    result = _run_json(cmd, timeout=30)
    result["command"] = "list-styles"
    _emit(result)


def cmd_bind_style(args: argparse.Namespace) -> None:
    bind = _run_json(
        [
            sys.executable,
            str(DRAFT_MANAGER),
            "update",
            "--draft",
            args.draft,
            "--set-style-id",
            args.style_id,
            "--edit-note",
            "helper bind-style: 用户选定风格",
            "--json",
        ],
        timeout=60,
    )
    ctx = _run_json(
        [
            sys.executable,
            str(STYLE_CLI),
            "get-context",
            "--style-id",
            args.style_id,
            "--format",
            "json",
        ],
        timeout=30,
    )
    if not ctx.get("ok"):
        _fail("STYLE_CONTEXT_FAILED", "get-context 返回 ok=false。", draft_id=args.draft, context=ctx)
    _emit(
        {
            "ok": True,
            "command": "bind-style",
            "result": {
                "draft_id": args.draft,
                "style_id": args.style_id,
                "style_name": ctx.get("style_name"),
                "set_style": bind.get("result"),
                "user_style_context": ctx.get("context_markdown"),
            },
            "summary": f"Draft #{args.draft} 已绑定风格 {ctx.get('style_name') or args.style_id}；可将 user_style_context 注入大纲/逐字稿生成。",
        }
    )


def cmd_prevalidate_script(args: argparse.Namespace) -> None:
    cmd = [
        sys.executable,
        str(DRAFT_MANAGER),
        "prevalidate",
        "--stage",
        "script_refining",
        "--payload-file",
        args.payload_file,
        "--json",
    ]
    if args.draft:
        cmd.extend(["--draft", args.draft])
    result = _run_json(cmd, timeout=60)
    result["command"] = "prevalidate-script"
    _emit(result)


def cmd_validate_script(args: argparse.Namespace) -> None:
    result = _run_json(
        [
            sys.executable,
            str(DRAFT_MANAGER),
            "update",
            "--draft",
            args.draft,
            "--stage",
            "script_refining",
            "--payload-file",
            args.payload_file,
            "--validate-only",
            "--json",
        ],
        timeout=60,
    )
    result["command"] = "validate-script"
    _emit(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="stream-gen safe workflow helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start-topic", help="create draft -> preflight -> topic_picking update")
    p_start.add_argument("--direction", required=True, help="用户原始开稿方向")
    p_start.add_argument("--topic", default=None, help="可选：create 时写入的初始 topic")
    p_start.add_argument("--style-id", default=None, help="可选：若用户已提前明确选择风格，可建稿时绑定")
    p_start.add_argument("--finance-root", default=None, help="可选：finance-source-ingest 根目录")
    p_start.add_argument("--timeout-sec", type=int, default=180, help="preflight 超时，默认 180s")
    p_start.add_argument(
        "--keep-run-artifacts",
        action="store_true",
        help="保留 /tmp/stream_gen_workflow 下的 topic_payload 副本（默认不落盘，仅用临时文件过 draft_manager）",
    )
    p_start.set_defaults(func=cmd_start_topic)

    p_choice = sub.add_parser(
        "apply-choice",
        help="set chosen -> build and persist evidence_pack（可从 Draft 自动读 topic/snapshot）",
    )
    p_choice.add_argument("--draft", required=True)
    p_choice.add_argument("--candidate-id", type=int, required=True, choices=[1, 2, 3])
    p_choice.add_argument(
        "--topic-payload-file",
        default=None,
        help="默认从 Draft 的 topic_candidates.json 读取",
    )
    p_choice.add_argument(
        "--snapshot-path",
        default=None,
        help=f"默认从 topic preflight_meta 或 {DEFAULT_SNAPSHOT_CACHE}",
    )
    p_choice.add_argument("--allow-targeted-fetch", action="store_true")
    p_choice.add_argument("--timeout-sec", type=int, default=180)
    p_choice.add_argument(
        "--skip-relevance-check",
        action="store_true",
        help="跳过候选-方向关联度预检（不推荐）",
    )
    p_choice.add_argument(
        "--min-relevance",
        type=float,
        default=0.10,
        help="关联度预检阈值，默认 0.10",
    )
    p_choice.add_argument(
        "--force-preflight",
        action="store_true",
        help="强制走 legacy：set-chosen + preflight --candidate-id（旧稿或无内嵌证据包时自动回退）",
    )
    p_choice.set_defaults(func=cmd_apply_choice)

    p_styles = sub.add_parser("list-styles", help="style_cli list --with-context wrapper")
    p_styles.add_argument("--with-context", action="store_true", default=True)
    p_styles.add_argument("--no-with-context", action="store_false", dest="with_context")
    p_styles.add_argument("--max-context-chars", type=int, default=480)
    p_styles.add_argument("--user-id", default=None)
    p_styles.set_defaults(func=cmd_list_styles)

    p_bind = sub.add_parser("bind-style", help="set-style-id + get-context in one step")
    p_bind.add_argument("--draft", required=True)
    p_bind.add_argument("--style-id", required=True)
    p_bind.set_defaults(func=cmd_bind_style)

    p_pre = sub.add_parser(
        "prevalidate-script",
        help="script payload schema check before generation (appendix/evidence rules)",
    )
    p_pre.add_argument("--payload-file", required=True)
    p_pre.add_argument("--draft", default=None, help="payload 缺 draft_id 时填入")
    p_pre.set_defaults(func=cmd_prevalidate_script)

    p_validate = sub.add_parser("validate-script", help="script_refining validate-only wrapper")
    p_validate.add_argument("--draft", required=True)
    p_validate.add_argument("--payload-file", required=True)
    p_validate.set_defaults(func=cmd_validate_script)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
