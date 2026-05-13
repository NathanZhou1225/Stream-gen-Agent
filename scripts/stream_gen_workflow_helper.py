#!/usr/bin/env python3
"""Safe workflow helper for stream-gen.

Bundles only non-decision steps. It deliberately stops at user choice gates:
- start-topic: create draft -> preflight -> topic_picking update
- apply-choice: set chosen -> build evidence_pack -> persist evidence_pack
- validate-script: wrapper around draft_manager update --validate-only
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
STREAMY_SCRIPTS = WORKSPACE_ROOT / "skills" / "streamy-content-gen" / "scripts"
DRAFT_MANAGER = STREAMY_SCRIPTS / "draft_manager.py"
PREFLIGHT_TOPIC = STREAMY_SCRIPTS / "preflight_topic.py"
RUN_ROOT = Path("/tmp/stream_gen_workflow")


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fail(code: str, message: str, **extra: Any) -> None:
    payload = {"ok": False, "error_code": code, "message": message}
    payload.update(extra)
    _emit(payload)
    raise SystemExit(1)


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


def cmd_start_topic(args: argparse.Namespace) -> None:
    create_cmd = [sys.executable, str(DRAFT_MANAGER), "create", "--json"]
    if args.topic:
        create_cmd.extend(["--topic", args.topic])
    if args.style_id:
        create_cmd.extend(["--style-id", args.style_id])
    create = _run_json(create_cmd, timeout=60)
    draft_id = _extract_draft_id(create)
    out_dir = _run_dir(draft_id)

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
    topic_payload_path = out_dir / "topic_payload.json"
    preflight_path = out_dir / "preflight_output.json"
    _write_json(topic_payload_path, topic_payload)
    _write_json(preflight_path, preflight)

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
            str(topic_payload_path),
            "--edit-note",
            "helper start-topic: preflight 后落盘三候选",
            "--json",
        ],
        timeout=60,
    )

    _emit(
        {
            "ok": True,
            "command": "start-topic",
            "result": {
                "draft_id": draft_id,
                "direction": args.direction,
                "run_dir": str(out_dir),
                "topic_payload_file": str(topic_payload_path),
                "preflight_output_file": str(preflight_path),
                "snapshot_path": preflight.get("snapshot_path"),
                "topic_update": update.get("result"),
                "candidates": topic_payload.get("candidates", []),
                "feishu_digest_bullets": topic_payload.get("feishu_digest_bullets", []),
            },
            "summary": f"已创建 Draft #{draft_id}，完成 preflight 并落盘 topic_picking；下一步等待用户选择 1/2/3。",
        }
    )


def cmd_apply_choice(args: argparse.Namespace) -> None:
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
            f"helper apply-choice: 选择候选 {args.candidate_id}",
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
        args.topic_payload_file,
        "--snapshot-path",
        args.snapshot_path,
    ]
    if args.allow_targeted_fetch:
        cmd.append("--allow-targeted-fetch")
    pack_result = _run_json(cmd, timeout=args.timeout_sec)
    if not pack_result.get("ok"):
        _fail("EVIDENCE_PACK_FAILED", "preflight evidence_pack 返回 ok=false。", draft_id=args.draft, preflight=pack_result)

    out_dir = _run_dir(args.draft)
    pack_path = out_dir / f"evidence_pack_C{args.candidate_id}.json"
    _write_json(pack_path, pack_result)

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
            f"helper apply-choice: 落盘候选 {args.candidate_id} 方向证据包",
            "--json",
        ],
        timeout=60,
    )

    _emit(
        {
            "ok": True,
            "command": "apply-choice",
            "result": {
                "draft_id": args.draft,
                "candidate_id": args.candidate_id,
                "run_dir": str(out_dir),
                "evidence_pack_file": str(pack_path),
                "set_chosen": set_chosen.get("result"),
                "persist": persist.get("result"),
                "evidence_pack": pack_result.get("evidence_pack", pack_result),
            },
            "summary": f"Draft #{args.draft} 已记录候选 {args.candidate_id} 并落盘方向证据包；下一步展示证据包并等待用户确认 user-style。",
        }
    )


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
    p_start.set_defaults(func=cmd_start_topic)

    p_choice = sub.add_parser("apply-choice", help="set chosen -> build and persist evidence_pack")
    p_choice.add_argument("--draft", required=True)
    p_choice.add_argument("--candidate-id", type=int, required=True, choices=[1, 2, 3])
    p_choice.add_argument("--topic-payload-file", required=True)
    p_choice.add_argument("--snapshot-path", required=True)
    p_choice.add_argument("--allow-targeted-fetch", action="store_true")
    p_choice.add_argument("--timeout-sec", type=int, default=180)
    p_choice.set_defaults(func=cmd_apply_choice)

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
