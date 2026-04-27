"""Resolve OpenClaw workspace root (same semantics as streamy-content-gen _common)."""

from __future__ import annotations

import os
from pathlib import Path


def get_user_id() -> str:
    return os.environ.get("OPENCLAW_USER_ID", "default")


def get_workspace_root() -> Path:
    """Match streamy `scripts/_common.get_workspace_root` for consistent user_data/ placement."""
    env = os.environ.get("OPENCLAW_WORKSPACE")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve().parent
    for cand in [here, *here.parents]:
        if (cand / "SKILL.md").is_file():
            continue
        if (cand / ".openclaw").is_dir() or (cand / "AGENTS.md").is_file():
            return cand
    return Path.cwd().resolve()


def get_user_data_dir() -> Path:
    return get_workspace_root() / "user_data"


def get_db_path() -> Path:
    return get_user_data_dir() / "style_memory.db"
