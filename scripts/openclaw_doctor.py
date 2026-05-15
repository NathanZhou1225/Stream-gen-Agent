#!/usr/bin/env python3
"""
Lightweight connectivity checks. Prints OPENCLAW_DIAG lines on failure.
Uses stdlib only; never prints secret values.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from openclaw_diag import classify_http_status, diag_from_http, emit_diag  # noqa: E402

import importlib.util

_spec = importlib.util.spec_from_file_location("verify_env", _SCRIPTS / "verify_env.py")
if _spec is None or _spec.loader is None:
    raise RuntimeError("verify_env not loadable")
_verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_verify)
merged_with_runtime = _verify.merged_with_runtime
run_checks = _verify.run_checks
is_set = _verify.is_set


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _json_req(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> tuple[int | None, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.getcode(), ""
    except urllib.error.HTTPError as e:
        try:
            snippet = e.read(400).decode("utf-8", errors="replace")
        except Exception:
            snippet = ""
        return e.code, snippet[:200]
    except Exception as e:
        return None, str(e)[:200]


def _get_req(url: str, timeout: float) -> tuple[int | None, str]:
    req = urllib.request.Request(url, method="GET")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.getcode(), ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return None, str(e)[:200]


def probe_openai_compat_chat(
    *,
    name: str,
    base_url: str,
    api_key: str,
    model: str,
    env_keys: list[str],
    timeout: float,
) -> bool:
    base = base_url.rstrip("/")
    url = f"{base}/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    status, snippet = _json_req(url, headers, body, timeout)
    if status == 200:
        print(f"doctor: {name} OK (HTTP 200).")
        return True
    code = classify_http_status(status)
    emit_diag(
        diag_from_http(
            code=code,
            integration=name,
            http_status=status,
            env_keys=env_keys,
            user_action="verify_url_or_rotate" if code == "AUTH_FAILED" else "check_quota_or_network",
            doc_anchor="DEPLOY.md#doctor",
            safe_hint=snippet or f"HTTP {status}",
        )
    )
    print(f"doctor: {name} failed (HTTP {status}).", file=sys.stderr)
    return False


def probe_rsshub(base_url: str) -> bool:
    u = base_url.rstrip("/") + "/"
    status, hint = _get_req(u, timeout=8.0)
    if status is not None and 200 <= status < 500:
        print("doctor: RSSHub base URL reachable.")
        return True
    code = classify_http_status(status)
    emit_diag(
        diag_from_http(
            code=code,
            integration="rsshub",
            http_status=status,
            env_keys=["FINANCE_RSSHUB_BASE_URL"],
            user_action="fix_base_url_or_bring_up_service",
            doc_anchor="DEPLOY.md#rsshub",
            safe_hint=hint or f"HTTP {status}",
        )
    )
    print("doctor: RSSHub probe failed.", file=sys.stderr)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="OpenClaw + stream-gen connectivity doctor.")
    ap.add_argument("--repo-root", type=Path, default=_default_repo_root())
    ap.add_argument("--skip-probes", action="store_true", help="Only run verify_env checks.")
    args = ap.parse_args()
    repo_root: Path = args.repo_root.resolve()
    env = merged_with_runtime(repo_root)

    ok, errs = run_checks(env)
    if not ok:
        for e in errs:
            print(e, file=sys.stderr)
        return 1

    if args.skip_probes:
        print("doctor: --skip-probes (no HTTP checks).")
        return 0

    failed = 0
    if is_set(env.get("DEEPSEEK_API_KEY", "")):
        base = env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip()
        model = env.get("DEEPSEEK_MODEL", "deepseek-v4-pro").strip()
        if not probe_openai_compat_chat(
            name="deepseek",
            base_url=base,
            api_key=env["DEEPSEEK_API_KEY"].strip(),
            model=model,
            env_keys=["DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"],
            timeout=25.0,
        ):
            failed += 1
    else:
        print("doctor: skip DeepSeek (DEEPSEEK_API_KEY unset).")

    if is_set(env.get("DASHSCOPE_CODING_API_KEY", "")):
        base = env.get("DASHSCOPE_CODING_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1").strip()
        model = env.get("DASHSCOPE_CODING_MODEL", "glm-5").strip()
        if not probe_openai_compat_chat(
            name="dashscope_coding",
            base_url=base,
            api_key=env["DASHSCOPE_CODING_API_KEY"].strip(),
            model=model,
            env_keys=["DASHSCOPE_CODING_API_KEY", "DASHSCOPE_CODING_BASE_URL", "DASHSCOPE_CODING_MODEL"],
            timeout=25.0,
        ):
            failed += 1
    else:
        print("doctor: skip Dashscope (DASHSCOPE_CODING_API_KEY unset).")

    if is_set(env.get("ARK_API_KEY", "")):
        base = env.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3").strip()
        model = env.get("ARK_MODEL_ID", "ark-code-latest").strip()
        if not probe_openai_compat_chat(
            name="ark_volcengine",
            base_url=base,
            api_key=env["ARK_API_KEY"].strip(),
            model=model,
            env_keys=["ARK_API_KEY", "ARK_BASE_URL", "ARK_MODEL_ID"],
            timeout=45.0,
        ):
            failed += 1
    else:
        print("doctor: skip Ark (ARK_API_KEY unset).")

    if is_set(env.get("FINANCE_LLM_ROUTER_API_KEY", "")):
        base = env.get("FINANCE_LLM_ROUTER_BASE_URL", "").strip().rstrip("/")
        model = env.get("FINANCE_LLM_ROUTER_MODEL", "deepseek-v4-flash").strip()
        if base:
            if not probe_openai_compat_chat(
                name="finance_llm_router",
                base_url=base,
                api_key=env["FINANCE_LLM_ROUTER_API_KEY"].strip(),
                model=model,
                env_keys=[
                    "FINANCE_LLM_ROUTER_API_KEY",
                    "FINANCE_LLM_ROUTER_BASE_URL",
                    "FINANCE_LLM_ROUTER_MODEL",
                ],
                timeout=float(env.get("FINANCE_LLM_ROUTER_TIMEOUT_SEC", "45") or 45),
            ):
                failed += 1
        else:
            emit_diag(
                {
                    "code": "BAD_ENDPOINT",
                    "integration": "finance_llm_router",
                    "http_status": None,
                    "env": ["FINANCE_LLM_ROUTER_BASE_URL"],
                    "user_action": "set_base_url",
                    "doc_anchor": "DEPLOY.md#finance-llm",
                    "safe_hint": "FINANCE_LLM_ROUTER_BASE_URL empty",
                }
            )
            failed += 1
    else:
        print("doctor: skip FINANCE_LLM_ROUTER (FINANCE_LLM_ROUTER_API_KEY unset).")

    if is_set(env.get("FINANCE_RSSHUB_BASE_URL", "")):
        if not probe_rsshub(env["FINANCE_RSSHUB_BASE_URL"].strip()):
            failed += 1
    else:
        print("doctor: skip RSSHub (FINANCE_RSSHUB_BASE_URL unset).")

    if failed:
        print(f"doctor: completed with {failed} failing probe(s).", file=sys.stderr)
        return 1
    print("doctor: all executed probes passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
