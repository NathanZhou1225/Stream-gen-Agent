#!/usr/bin/env python3
"""
Post-verify readiness: P1 云端 Newsbox API。不打印密钥。
缺 P1 且未设置 STREAM_GEN_SKIP_P1_READINESS=1 时 exit 1。
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import importlib.util

_scripts = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("verify_env_mod", _scripts / "verify_env.py")
if _spec is None or _spec.loader is None:
    print("deploy_readiness: cannot load verify_env.py", file=sys.stderr)
    sys.exit(2)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
merged_with_runtime = _mod.merged_with_runtime
is_set = _mod.is_set


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _probe_cloud_health(base_url: str, api_key: str, timeout_sec: int) -> tuple[bool, str]:
    base = base_url.strip().rstrip("/")
    url = f"{base}/health"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} from {url}"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None) or exc
        return False, f"无法连接 {url}: {reason}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300]
    if '"ok":true' in body.replace(" ", "") or '"ok": true' in body:
        return True, ""
    return False, f"health 响应异常: {body[:200]}"


def collect_p1_gaps(env: dict[str, str]) -> list[tuple[str, str, str]]:
    gaps: list[tuple[str, str, str]] = []
    base = str(env.get("FINANCE_CLOUD_API_BASE_URL", "") or "")
    key = str(env.get("FINANCE_CLOUD_API_KEY", "") or "")
    if not is_set(base):
        gaps.append(
            (
                "cloud_api_base",
                "FINANCE_CLOUD_API_BASE_URL",
                "未配置云端 API 基址（见 DEPLOY.md）。",
            )
        )
    if not is_set(key):
        gaps.append(
            (
                "cloud_api_key",
                "FINANCE_CLOUD_API_KEY",
                "未配置云端 Bearer Key（与 finance-ingest-cloud 的 FINANCE_CLOUD_API_KEYS 一致）。",
            )
        )
    if gaps:
        return gaps
    try:
        timeout_sec = max(3, int(str(env.get("FINANCE_CLOUD_API_TIMEOUT", "15") or "15")))
    except ValueError:
        timeout_sec = 15
    ok, err = _probe_cloud_health(base, key, timeout_sec)
    if not ok:
        gaps.append(
            (
                "cloud_api_unreachable",
                "FINANCE_CLOUD_API_BASE_URL,FINANCE_CLOUD_API_KEY",
                f"云端 /health 探测失败：{err}",
            )
        )
    return gaps


def collect_optional_notes(env: dict[str, str]) -> list[str]:
    notes = [
        "P1：云端 Newsbox（FINANCE_CLOUD_API_BASE_URL + FINANCE_CLOUD_API_KEY）；"
        "TUSHARE/RSSHub/ingest 仅在 finance-ingest-cloud Worker。"
    ]
    feishu_ok = is_set(str(env.get("FEISHU_APP_ID", "") or "")) and is_set(
        str(env.get("FEISHU_APP_SECRET", "") or "")
    )
    if not feishu_ok:
        notes.append(
            "飞书应用（FEISHU_APP_ID / FEISHU_APP_SECRET）未配置：不影响 OpenClaw 内对话；仅飞书通道需要。"
        )
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(description="P1 readiness: cloud Newsbox API only.")
    ap.add_argument("--repo-root", type=Path, default=_default_repo_root())
    args = ap.parse_args()
    env = merged_with_runtime(args.repo_root.resolve())

    p1 = collect_p1_gaps(env)
    for code, keys, msg in p1:
        print(f"[P1_GAP] code={code} env={keys} message={msg}")

    for line in collect_optional_notes(env):
        print(f"[OPTIONAL] {line}")

    skip = str(
        env.get("STREAM_GEN_SKIP_P1_READINESS", "")
        or os.environ.get("STREAM_GEN_SKIP_P1_READINESS", "")
        or "",
    ).strip()
    if p1 and skip.lower() not in ("1", "true", "yes", "on"):
        print(
            "[P1_GAP] summary=存在主链路 P1 缺口；请补全 FINANCE_CLOUD_API_*，"
            "或临时 STREAM_GEN_SKIP_P1_READINESS=1（不推荐生产）。",
            file=sys.stderr,
        )
        return 1

    if p1:
        print("[DEPLOY_READINESS] p1=skipped_by_STREAM_GEN_SKIP_P1_READINESS mode=cloud optional=see_above")
    else:
        print("[DEPLOY_READINESS] p1=ok mode=cloud optional=see_above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
