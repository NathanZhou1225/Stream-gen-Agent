#!/usr/bin/env python3
"""
Post-verify readiness: P1 信源 / 云 API、选配说明。不打印密钥。
若存在 P1 缺口且未设置 STREAM_GEN_SKIP_P1_READINESS=1，以 exit 1 结束，避免「部署成功」假阳性。

v0.3.0：FINANCE_CLOUD_MODE=1（默认推荐）时 P1 校验云端 API；本地 ingest 见 FINANCE_CLOUD_MODE=0 + TUSHARE/RSSHub。
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


def cloud_mode_enabled(env: dict[str, str]) -> bool:
    """客户端是否走云端 Newsbox（默认：未显式关闭且已配 API 则视为云模式）。"""
    v = str(env.get("FINANCE_CLOUD_MODE", "") or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return is_set(str(env.get("FINANCE_CLOUD_API_BASE_URL", "") or "")) and is_set(
        str(env.get("FINANCE_CLOUD_API_KEY", "") or "")
    )


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


def collect_p1_gaps_cloud(env: dict[str, str]) -> list[tuple[str, str, str]]:
    gaps: list[tuple[str, str, str]] = []
    base = str(env.get("FINANCE_CLOUD_API_BASE_URL", "") or "")
    key = str(env.get("FINANCE_CLOUD_API_KEY", "") or "")
    if not is_set(base):
        gaps.append(
            (
                "cloud_api_base",
                "FINANCE_CLOUD_API_BASE_URL",
                "未配置云端 API 基址（外网示例 http://<公网IP>:8080；见 DEPLOY.md）。",
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
                f"云端 /health 探测失败：{err}（确认 API 已启动、安全组已放行 8080）。",
            )
        )
    return gaps


def collect_p1_gaps_local(env: dict[str, str]) -> list[tuple[str, str, str]]:
    """Advanced：本地 ingest + SQLite（FINANCE_CLOUD_MODE=0）。"""
    gaps: list[tuple[str, str, str]] = []
    if not is_set(str(env.get("FINANCE_RSSHUB_BASE_URL", "") or "")):
        gaps.append(
            (
                "rsshub",
                "FINANCE_RSSHUB_BASE_URL",
                "本地模式未配置 RSSHub（Advanced）；云模式请设 FINANCE_CLOUD_MODE=1。",
            )
        )
    if not is_set(str(env.get("TUSHARE_TOKEN", "") or "")):
        gaps.append(
            (
                "tushare",
                "TUSHARE_TOKEN",
                "本地模式未配置 Tushare（Advanced）；云模式请设 FINANCE_CLOUD_MODE=1。",
            )
        )
    return gaps


def collect_p1_gaps(env: dict[str, str]) -> list[tuple[str, str, str]]:
    if cloud_mode_enabled(env):
        return collect_p1_gaps_cloud(env)
    return collect_p1_gaps_local(env)


def collect_optional_notes(env: dict[str, str]) -> list[str]:
    notes: list[str] = []
    if cloud_mode_enabled(env):
        notes.append(
            "P1 路径：云端 Newsbox（FINANCE_CLOUD_MODE=1）；TUSHARE/RSSHub 仅在云端 Worker 配置，客户端无需填写。"
        )
    else:
        notes.append(
            "P1 路径：本地 ingest（FINANCE_CLOUD_MODE=0）；需本机 TUSHARE + RSSHub + 定时 ingest。"
        )
    feishu_ok = is_set(str(env.get("FEISHU_APP_ID", "") or "")) and is_set(
        str(env.get("FEISHU_APP_SECRET", "") or "")
    )
    if not feishu_ok:
        notes.append(
            "飞书应用（FEISHU_APP_ID / FEISHU_APP_SECRET）未配置：不影响 WorkBuddy/OpenClaw 内对话；"
            "仅在使用飞书通道时需要。"
        )
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(
        description="P1 readiness: cloud API (default) or local RSSHub/Tushare (advanced).",
    )
    ap.add_argument("--repo-root", type=Path, default=_default_repo_root())
    args = ap.parse_args()
    repo_root: Path = args.repo_root.resolve()
    env = merged_with_runtime(repo_root)

    mode = "cloud" if cloud_mode_enabled(env) else "local"
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
            "[P1_GAP] summary=存在主链路 P1 缺口；请补全上述变量，或临时设置 "
            "STREAM_GEN_SKIP_P1_READINESS=1 后再跑安装（不推荐生产）。",
            file=sys.stderr,
        )
        return 1

    if p1:
        print(f"[DEPLOY_READINESS] p1=skipped_by_STREAM_GEN_SKIP_P1_READINESS mode={mode} optional=see_above")
    else:
        print(f"[DEPLOY_READINESS] p1=ok mode={mode} optional=see_above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
