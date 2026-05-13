#!/usr/bin/env python3
"""
最小上下文重置工具（stream-gen）

目标：
- 不动 drafts / memory / rules；
- 仅清理会话层历史（sessions/*.jsonl、*.reset*）；
- sessions.json.backup* 也会移出活跃 sessions 树；
- 默认保留最近 1 条会话，避免当前会话句柄失联。
- 旧会话搬到 sessions 的兄弟目录 session_archives/，避免继续被 sessions 递归扫描。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_SESSIONS_DIR = Path("/root/.openclaw/agents/stream-gen/sessions")
DEFAULT_ARCHIVE_ROOT = Path("/root/.openclaw/agents/stream-gen/session_archives")


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _load_sessions_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _latest_jsonl_files(sessions_dir: Path) -> list[Path]:
    return sorted(
        [p for p in sessions_dir.glob("*.jsonl") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _resolve_keep_set(sessions_dir: Path, keep_latest: int) -> set[Path]:
    keep: set[Path] = set()
    latest = _latest_jsonl_files(sessions_dir)[: max(0, keep_latest)]
    keep.update(latest)

    # 额外保留 sessions.json 中登记的 sessionFile（若存在）
    sj = _load_sessions_json(sessions_dir / "sessions.json")
    for v in sj.values():
        if not isinstance(v, dict):
            continue
        sf = v.get("sessionFile")
        if isinstance(sf, str) and sf.strip():
            p = Path(sf)
            if p.is_file():
                keep.add(p)
    return keep


def _archive_move(files: list[Path], archive_dir: Path) -> list[str]:
    moved: list[str] = []
    archive_dir.mkdir(parents=True, exist_ok=True)
    for p in files:
        target = archive_dir / p.name
        if target.exists():
            target = archive_dir / f"{p.stem}-{_now_tag()}{p.suffix}"
        shutil.move(str(p), str(target))
        moved.append(f"{p.name} -> {target.name}")
    return moved


def _compact_sessions_json(sessions_dir: Path) -> tuple[int, int]:
    """
    删除 sessions.json 中指向已不存在 sessionFile 的陈旧条目，减少基线上下文体积。
    """
    path = sessions_dir / "sessions.json"
    data = _load_sessions_json(path)
    if not data:
        return (0, 0)

    before = len(data)
    kept: dict[str, Any] = {}
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        sf = val.get("sessionFile")
        if isinstance(sf, str) and sf.strip():
            if Path(sf).is_file():
                kept[key] = val
            continue
        # 没有 sessionFile 的条目默认保留
        kept[key] = val
    if kept != data:
        path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    return (before, len(kept))


def _slim_skill_snapshot(sessions_dir: Path) -> dict[str, Any]:
    script = Path(__file__).resolve().parent / "slim_skill_snapshot.py"
    if not script.is_file():
        return {"ok": False, "error": f"missing script: {script}"}
    try:
        cp = subprocess.run(
            [sys.executable, str(script), "--sessions-json", str(sessions_dir / "sessions.json")],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if cp.returncode != 0:
            return {"ok": False, "error": (cp.stderr or cp.stdout or "").strip()[:500]}
        out = (cp.stdout or "").strip()
        return json.loads(out) if out else {"ok": False, "error": "empty output"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    p = argparse.ArgumentParser(description="最小上下文重置：仅清理会话历史")
    p.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    p.add_argument(
        "--archive-root",
        type=Path,
        default=DEFAULT_ARCHIVE_ROOT,
        help="旧会话归档根目录（默认放在 sessions 兄弟目录，避免被 sessions 递归扫描）",
    )
    p.add_argument("--keep-latest", type=int, default=1, help="保留最近 N 条 jsonl（默认 1）")
    p.add_argument("--compact-sessions-json", action="store_true", help="清理 sessions.json 中失效条目")
    p.add_argument("--no-slim-skill-snapshot", action="store_true", help="不裁剪 stream-gen skillsSnapshot")
    p.add_argument("--dry-run", action="store_true", help="仅预览，不实际移动")
    args = p.parse_args()

    sessions_dir = args.sessions_dir.resolve()
    if not sessions_dir.is_dir():
        print(
            json.dumps(
                {"ok": False, "error": f"sessions 目录不存在: {sessions_dir}"},
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(0)

    keep_set = _resolve_keep_set(sessions_dir, args.keep_latest)
    jsonl_files = [p for p in sessions_dir.glob("*.jsonl") if p.is_file()]
    reset_files = [p for p in sessions_dir.glob("*.reset*") if p.is_file()]
    backup_files = [p for p in sessions_dir.glob("sessions.json.backup*") if p.is_file()]
    to_move = [p for p in jsonl_files if p not in keep_set] + reset_files + backup_files

    archive_dir = args.archive_root.resolve() / _now_tag()
    moved: list[str] = []
    if (not args.dry_run) and to_move:
        moved = _archive_move(sorted(to_move), archive_dir)

    compact_before = compact_after = 0
    if args.compact_sessions_json and (not args.dry_run):
        compact_before, compact_after = _compact_sessions_json(sessions_dir)

    slim_result: dict[str, Any] | None = None
    if (not args.no_slim_skill_snapshot) and (not args.dry_run):
        slim_result = _slim_skill_snapshot(sessions_dir)

    result = {
        "ok": True,
        "sessions_dir": str(sessions_dir),
        "keep_latest": max(0, args.keep_latest),
        "dry_run": bool(args.dry_run),
        "total_jsonl": len(jsonl_files),
        "kept_jsonl": len([p for p in jsonl_files if p in keep_set]),
        "to_archive_count": len(to_move),
        "archive_dir": str(archive_dir),
        "moved": moved[:50],
        "sessions_json_compact": {
            "before": compact_before,
            "after": compact_after,
        },
        "skill_snapshot_slim": slim_result,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

