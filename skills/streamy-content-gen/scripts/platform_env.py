"""WorkBuddy / Windows 平台环境：UTF-8 模式、.env 合并、快照 cache 版本。"""
from __future__ import annotations

import os
import sys

# 升级此版本后，旧 cache（无该 meta 或版本更低）视为 stale
SNAPSHOT_CACHE_ENCODING_VERSION = 2

# Windows CreateProcess 单环境变量值上限约 32767 字符
WIN_ENV_VALUE_MAX_CHARS = 32_766


def apply_python_utf8_mode() -> None:
    """强制 Python UTF-8 模式（Windows 区域 GBK 下避免子进程/写 cache 乱码）。"""
    os.environ["PYTHONUTF8"] = "1"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def safe_update_os_environ(overlay: dict[str, str]) -> list[str]:
    """
    将 .env 合并进 os.environ，跳过超长值（Windows 会 OSError）。
    返回被跳过的键名列表（便于日志）。
    """
    skipped: list[str] = []
    for key, value in overlay.items():
        if not key:
            continue
        sval = str(value)
        if len(sval) > WIN_ENV_VALUE_MAX_CHARS:
            skipped.append(key)
            continue
        os.environ[key] = sval
    return skipped


def cache_encoding_version_ok(meta: dict) -> bool:
    try:
        ver = int((meta or {}).get("snapshot_cache_encoding_version") or 0)
    except (TypeError, ValueError):
        ver = 0
    return ver >= SNAPSHOT_CACHE_ENCODING_VERSION


def windows_utf8_warnings() -> list[str]:
    """非致命提示：供 verify_env / bootstrap 打印。"""
    if sys.platform != "win32":
        return []
    warns: list[str] = []
    if os.environ.get("PYTHONUTF8", "").strip() not in ("1", "true", "yes", "on"):
        warns.append(
            "WINDOWS_HINT: 未设置 PYTHONUTF8=1，拉数/cache 可能出现中文乱码。"
            "建议用户环境变量永久设置 PYTHONUTF8=1，或使用 scripts/query_market_facts.ps1。"
        )
    return warns
