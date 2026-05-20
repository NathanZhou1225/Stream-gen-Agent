#!/usr/bin/env python3
"""Verify merged .env + optional process env (OpenClaw host injection). Never prints secret values."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPTS_STREAMY = Path(__file__).resolve().parent.parent / "skills" / "streamy-content-gen" / "scripts"
if _SCRIPTS_STREAMY.is_dir() and str(_SCRIPTS_STREAMY) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_STREAMY))

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


def merged_with_runtime(repo_root: Path) -> dict[str, str]:
    """磁盘 .env 合并后，用当前进程环境补齐「未在文件中出现或为空」的键（宿主 OpenClaw 可注入 OPENCLAW_* 等）。"""
    out = merge_env(repo_root)
    for k, v in os.environ.items():
        if v is None or not str(v).strip():
            continue
        cur = str(out.get(k, "") or "").strip()
        if not cur:
            out[k] = str(v).strip()
    return out


def _first_nonempty(env: dict[str, str], keys: tuple[str, ...]) -> str:
    for n in keys:
        v = str(env.get(n, "") or "").strip()
        if is_set(v):
            return v
    return ""


def _truthy_switch(val: str, *, default: str = "0") -> bool:
    raw = str(val if val is not None else "").strip()
    if not raw:
        raw = default
    return raw.lower() in ("1", "true", "yes", "on")


def _clean_enabled(env: dict[str, str]) -> bool:
    """与 finance-source-ingest/cleaner.py 一致：默认开启，除非显式关。"""
    raw = str(env.get("FINANCE_INGEST_LLM_CLEAN_ENABLED", "1")).strip()
    return raw.lower() not in ("0", "false", "no", "off")


def clean_effective_trio(env: dict[str, str]) -> tuple[str, str, str]:
    """与 cleaner._load 同源回退链。"""
    base = _first_nonempty(
        env,
        (
            "FINANCE_INGEST_LLM_CLEAN_BASE_URL",
            "OPENCLAW_ARK_BASE_URL",
            "OPENCLAW_ARK_ENDPOINT",
        ),
    )
    key = _first_nonempty(
        env,
        (
            "FINANCE_INGEST_LLM_CLEAN_API_KEY",
            "OPENCLAW_ARK_API_KEY",
            "ARK_API_KEY",
        ),
    )
    model = _first_nonempty(
        env,
        (
            "FINANCE_INGEST_LLM_CLEAN_MODEL",
            "OPENCLAW_ARK_MODEL",
            "ARK_MODEL_ID",
        ),
    )
    return base, key, model


def router_effective_trio(env: dict[str, str]) -> tuple[str, str, str]:
    """与 router._load_router_config 同源回退链。"""
    base = _first_nonempty(
        env,
        ("FINANCE_LLM_ROUTER_BASE_URL", "OPENCLAW_ARK_BASE_URL", "OPENCLAW_ARK_ENDPOINT"),
    )
    key = _first_nonempty(env, ("FINANCE_LLM_ROUTER_API_KEY", "OPENCLAW_ARK_API_KEY", "ARK_API_KEY"))
    model = _first_nonempty(env, ("FINANCE_LLM_ROUTER_MODEL", "OPENCLAW_ARK_MODEL", "ARK_MODEL_ID"))
    return base, key, model


def rewrite_effective_trio(env: dict[str, str]) -> tuple[str, str, str]:
    """与 rewriter._load_config 同源回退链。"""
    base = _first_nonempty(
        env,
        (
            "FINANCE_SECTOR_LLM_BASE_URL",
            "FINANCE_LLM_ROUTER_BASE_URL",
            "FINANCE_INGEST_LLM_CLEAN_BASE_URL",
            "OPENCLAW_ARK_BASE_URL",
            "OPENCLAW_ARK_ENDPOINT",
        ),
    )
    key = _first_nonempty(
        env,
        (
            "FINANCE_SECTOR_LLM_API_KEY",
            "FINANCE_LLM_ROUTER_API_KEY",
            "FINANCE_INGEST_LLM_CLEAN_API_KEY",
            "OPENCLAW_ARK_API_KEY",
            "ARK_API_KEY",
        ),
    )
    model = _first_nonempty(
        env,
        (
            "FINANCE_SECTOR_LLM_MODEL",
            "FINANCE_LLM_ROUTER_MODEL",
            "FINANCE_INGEST_LLM_CLEAN_MODEL",
            "OPENCLAW_ARK_MODEL",
            "ARK_MODEL_ID",
        ),
    )
    return base, key, model


def run_checks(env: dict[str, str]) -> tuple[bool, list[str]]:
    errors: list[str] = []

    # v0.3.0 客户端：ingest LLM 清洗仅在 finance-ingest-cloud Worker；本仓只校验 Router/Rewriter。
    # 若显式 FINANCE_INGEST_LLM_CLEAN_ENABLED=1 且在本机跑 legacy/cleaner，仍校验三件套（高级用法）。
    if _clean_enabled(env) and _truthy_switch(
        str(env.get("FINANCE_INGEST_LLM_CLEAN_ENABLED", "0")).strip(), default="0"
    ):
        b, k, m = clean_effective_trio(env)
        if not (is_set(b) and is_set(k) and is_set(m)):
            errors.append(
                "MISSING_CONFIG: ingest_llm_clean — FINANCE_INGEST_LLM_CLEAN_ENABLED=1 但未解析到完整的 "
                "BASE_URL + API_KEY + MODEL。常规部署请删除该行或设为 0（清洗在云端）；"
                "仅 --live-fetch / 本机 legacy 运维需要时保留并填写 FINANCE_INGEST_LLM_CLEAN_*。"
            )

    if _truthy_switch(env.get("FINANCE_LLM_ROUTER_ENABLED", "1")):
        b, k, m = router_effective_trio(env)
        if not (is_set(b) and is_set(k) and is_set(m)):
            errors.append(
                "MISSING_CONFIG: llm_router — FINANCE_LLM_ROUTER_ENABLED 默认开启，但未解析到完整的 "
                "BASE_URL + API_KEY + MODEL。请填写 FINANCE_LLM_ROUTER_* 或依赖宿主 OPENCLAW_ARK_* / "
                "ARK_API_KEY；不需要 Router 时请显式设 FINANCE_LLM_ROUTER_ENABLED=0。详见 .env.example。"
            )

    if _truthy_switch(env.get("FINANCE_SECTOR_LLM_REWRITE_ENABLED", "1")):
        b, k, m = rewrite_effective_trio(env)
        if not (is_set(b) and is_set(k) and is_set(m)):
            errors.append(
                "MISSING_CONFIG: sector_llm_rewrite — FINANCE_SECTOR_LLM_REWRITE_ENABLED 默认开启，但未解析到完整的 "
                "BASE_URL + API_KEY + MODEL。请填写 FINANCE_SECTOR_LLM_*，或与 Router/Clean 共用 "
                "FINANCE_LLM_ROUTER_* / FINANCE_INGEST_LLM_CLEAN_* / 宿主 OPENCLAW_ARK_*；"
                "不需要板块润色时请显式设 FINANCE_SECTOR_LLM_REWRITE_ENABLED=0。详见 .env.example。"
            )

    return (len(errors) == 0, errors)


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify finance LLM feature gates in .env (+ runtime).")
    ap.add_argument(
        "--repo-root",
        type=Path,
        default=_default_repo_root(),
        help="Workspace root: standalone stream-gen repo, or OpenClaw monorepo root.",
    )
    args = ap.parse_args()
    repo_root: Path = args.repo_root.resolve()
    env = merged_with_runtime(repo_root)
    ok, errs = run_checks(env)
    try:
        from platform_env import windows_utf8_warnings

        for w in windows_utf8_warnings():
            print(f"verify_env: {w}", file=sys.stderr)
    except ImportError:
        pass
    if ok:
        print("verify_env: OK (finance LLM feature gates satisfied).")
        return 0
    for e in errs:
        print(f"verify_env: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
