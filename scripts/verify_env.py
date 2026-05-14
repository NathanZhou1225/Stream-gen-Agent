#!/usr/bin/env python3
"""Verify merged .env (standalone repo or OpenClaw monorepo). Never prints secret values."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PLACEHOLDERS = frozenset(
    {
        "",
        "changeme",
        "change_me",
        "your_key_here",
        "xxx",
        "sk-xxx",
    }
)


def _strip_quotes(val: str) -> str:
    v = val.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1].strip()
    return v


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    raw = path.read_text(encoding="utf-8", errors="replace")
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, _, rest = s.partition("=")
        key = key.strip()
        if not key:
            continue
        val = _strip_quotes(rest)
        out[key] = val
    return out


def is_set(val: str) -> bool:
    v = val.strip()
    if not v:
        return False
    if v.lower() in _PLACEHOLDERS:
        return False
    return True


def merge_env(repo_root: Path) -> dict[str, str]:
    """
    - Monorepo (OpenClaw root): repo_root/.env then repo_root/workspace-stream-gen/.env (override).
    - Monorepo child dir: repo_root is workspace-stream-gen; parent/openclaw.json → merge parent/.env then repo_root/.env.
    - Standalone GitHub (repo = stream-gen only): only repo_root/.env.
    """
    root = repo_root.resolve()
    merged: dict[str, str] = {}
    nested = root / "workspace-stream-gen"
    if nested.is_dir():
        merged.update(load_env_file(root / ".env"))
        merged.update(load_env_file(nested / ".env"))
        return merged
    parent = root.parent
    if (parent / "openclaw.json").is_file():
        merged.update(load_env_file(parent / ".env"))
        merged.update(load_env_file(root / ".env"))
        return merged
    merged.update(load_env_file(root / ".env"))
    return merged


def run_checks(env: dict[str, str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    llm_keys = ("ARK_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_CODING_API_KEY")
    if not any(is_set(env.get(k, "")) for k in llm_keys):
        errors.append(
            "MISSING_REQUIRED_GROUP: llm — set at least one of: "
            + ", ".join(llm_keys)
            + " (see .env.example)"
        )
    return (len(errors) == 0, errors)


def _default_repo_root() -> Path:
    # .../workspace-stream-gen/scripts/verify_env.py → workspace-stream-gen
    return Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify required env vars are present.")
    ap.add_argument(
        "--repo-root",
        type=Path,
        default=_default_repo_root(),
        help="Workspace root: standalone stream-gen repo, or OpenClaw monorepo root.",
    )
    args = ap.parse_args()
    repo_root: Path = args.repo_root.resolve()
    env = merge_env(repo_root)
    ok, errs = run_checks(env)
    if ok:
        print("verify_env: OK (required LLM credential group satisfied).")
        return 0
    for e in errs:
        print(f"verify_env: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
