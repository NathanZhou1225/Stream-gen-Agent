#!/usr/bin/env python3
"""
裁剪 stream-gen 当前 sessions.json 里的 skillsSnapshot。

用途：
- openclaw.json 的 agent.skills 白名单会影响新建/刷新后的会话；
- 已存在 session 里的 skillsSnapshot 不一定立刻重建；
- 本脚本对现有 sessions.json 做一次安全裁剪，避免继续携带全局技能快照。
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_SESSIONS_JSON = Path("/root/.openclaw/agents/stream-gen/sessions/sessions.json")
DEFAULT_ALLOWED = [
    "finance-source-ingest",
    "tavily-search",
    "streamy-content-gen",
    "user-style-manager",
    "self-improvement",
]

KNOWN_SKILL_FILES = {
    "finance-source-ingest": Path("/root/.openclaw/workspace-stream-gen/skills/finance-source-ingest/SKILL.md"),
    "tavily-search": Path("/root/.openclaw/workspace-stream-gen/skills/liang-tavily-search-1.0.1/SKILL.md"),
    "streamy-content-gen": Path("/root/.openclaw/workspace-stream-gen/skills/streamy-content-gen/SKILL.md"),
    "user-style-manager": Path("/root/.openclaw/workspace-stream-gen/skills/user-style-manager/SKILL.md"),
    "self-improvement": Path("/root/.openclaw/skills/self-improving-agent/SKILL.md"),
}


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _skill_name(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("name") or "").strip()
    return ""


def _read_frontmatter_skill(skill_path: Path) -> dict[str, Any] | None:
    if not skill_path.is_file():
        return None
    text = skill_path.read_text(encoding="utf-8")
    name = skill_path.parent.name
    description = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end].strip().splitlines()
            for raw in fm:
                line = raw.strip()
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"').strip("'") or name
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"').strip("'")
    if not description:
        for raw in text.splitlines():
            s = raw.strip()
            if s and not s.startswith("---") and not s.startswith("#"):
                description = s[:300]
                break
    return {
        "name": name,
        "description": description,
        "filePath": str(skill_path),
        "baseDir": str(skill_path.parent),
    }


def _ensure_allowed_entries(existing: list[dict[str, Any]], allowed_order: list[str]) -> list[dict[str, Any]]:
    by_name = {_skill_name(x): x for x in existing if _skill_name(x)}
    out: list[dict[str, Any]] = []
    for name in allowed_order:
        row = by_name.get(name)
        if row is None:
            row = _read_frontmatter_skill(KNOWN_SKILL_FILES.get(name, Path()))
        if isinstance(row, dict):
            out.append(row)
    return out


def _build_prompt(resolved: list[dict[str, Any]]) -> str:
    lines: list[str] = [
        "",
        "",
        "The following skills provide specialized instructions for stream-gen only.",
        "Use the read tool to load a skill's file when the task matches its description.",
        "",
        "<available_skills>",
    ]
    for row in resolved:
        name = str(row.get("name") or "").strip()
        desc = str(row.get("description") or "").strip()
        path = str(row.get("filePath") or row.get("location") or "").strip()
        if not name:
            continue
        lines.extend(
            [
                "  <skill>",
                f"    <name>{name}</name>",
                f"    <description>{desc}</description>",
                f"    <location>{path}</location>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Slim stream-gen skillsSnapshot to a whitelist")
    p.add_argument("--sessions-json", type=Path, default=DEFAULT_SESSIONS_JSON)
    p.add_argument("--allowed", nargs="*", default=DEFAULT_ALLOWED)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    path = args.sessions_json.resolve()
    allowed_order = [x.strip() for x in args.allowed if x.strip()]
    allowed = set(allowed_order)
    if not path.is_file():
        print(json.dumps({"ok": False, "error": f"sessions.json not found: {path}"}, ensure_ascii=False, indent=2))
        raise SystemExit(0)

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print(json.dumps({"ok": False, "error": "sessions.json is not an object"}, ensure_ascii=False, indent=2))
        raise SystemExit(0)

    changed = False
    reports: list[dict[str, Any]] = []
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        snapshot = entry.get("skillsSnapshot")
        if not isinstance(snapshot, dict):
            continue
        before_prompt_chars = len(str(snapshot.get("prompt") or ""))
        skills = snapshot.get("skills")
        resolved = snapshot.get("resolvedSkills")
        new_skills = [x for x in skills if _skill_name(x) in allowed] if isinstance(skills, list) else []
        existing_resolved = [x for x in resolved if _skill_name(x) in allowed] if isinstance(resolved, list) else []
        new_resolved = _ensure_allowed_entries(existing_resolved, allowed_order)
        resolved_names = {_skill_name(x) for x in new_resolved}
        new_skills = [x for x in new_skills if _skill_name(x) in resolved_names]
        for row in new_resolved:
            name = _skill_name(row)
            if name and name not in {_skill_name(x) for x in new_skills}:
                new_skills.append({"name": name})
        if new_resolved:
            snapshot["prompt"] = _build_prompt(new_resolved)
        snapshot["skills"] = new_skills
        snapshot["resolvedSkills"] = new_resolved
        snapshot["skillFilter"] = list(allowed_order)
        after_prompt_chars = len(str(snapshot.get("prompt") or ""))
        changed = True
        reports.append(
            {
                "session_key": key,
                "skills_before": len(skills) if isinstance(skills, list) else None,
                "skills_after": len(new_skills),
                "resolved_before": len(resolved) if isinstance(resolved, list) else None,
                "resolved_after": len(new_resolved),
                "prompt_chars_before": before_prompt_chars,
                "prompt_chars_after": after_prompt_chars,
            }
        )

    if changed and not args.dry_run:
        backup = path.with_name(f"{path.name}.pre-slim-{_now_tag()}")
        shutil.copy2(path, backup)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": bool(args.dry_run),
                "sessions_json": str(path),
                "allowed": list(allowed_order),
                "changed": changed,
                "reports": reports,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

