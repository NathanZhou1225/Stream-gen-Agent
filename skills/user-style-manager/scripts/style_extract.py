"""Call Ark (OpenAI-compatible) to extract style JSON from raw transcript text."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from paths import get_user_data_dir

_SKILLS_ROOT = Path(__file__).resolve().parent.parent
_PROMPT_FILE = _SKILLS_ROOT / "prompts" / "extract-style.md"
_REFINE_PROMPT_FILE = _SKILLS_ROOT / "prompts" / "refine-style.md"

# Bump when extract/refine prompts or profile schema change enough to invalidate old cache entries.
_EXTRACT_CACHE_SCHEMA = 1
_REFINE_CACHE_SCHEMA = 1


def _cache_enabled() -> bool:
    v = os.environ.get("STYLE_EXTRACT_CACHE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _cache_dir() -> Path:
    d = get_user_data_dir() / "style_extract_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_extract_cache(key_hex: str) -> dict[str, Any] | None:
    p = _cache_dir() / f"extract_{key_hex}.json"
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if raw.get("schema") != _EXTRACT_CACHE_SCHEMA:
        return None
    prof = raw.get("profile")
    if not isinstance(prof, dict):
        return None
    return {
        "profile": prof,
        "raw_model": raw.get("raw_model") or "",
    }


def _write_extract_cache(key_hex: str, profile: dict[str, Any], raw_model: str) -> None:
    p = _cache_dir() / f"extract_{key_hex}.json"
    payload = {
        "schema": _EXTRACT_CACHE_SCHEMA,
        "profile": profile,
        "raw_model": (raw_model or "")[:8000],
    }
    try:
        fd, tmp = tempfile.mkstemp(
            suffix=".json", dir=_cache_dir(), text=True
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        Path(tmp).replace(p)
    except OSError:
        pass


def _read_refine_cache(key_hex: str) -> dict[str, Any] | None:
    p = _cache_dir() / f"refine_{key_hex}.json"
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if raw.get("schema") != _REFINE_CACHE_SCHEMA:
        return None
    prof = raw.get("profile")
    if not isinstance(prof, dict):
        return None
    return {
        "profile": prof,
        "raw_model": raw.get("raw_model") or "",
    }


def _write_refine_cache(
    key_hex: str, profile: dict[str, Any], raw_model: str
) -> None:
    p = _cache_dir() / f"refine_{key_hex}.json"
    payload = {
        "schema": _REFINE_CACHE_SCHEMA,
        "profile": profile,
        "raw_model": (raw_model or "")[:8000],
    }
    try:
        fd, tmp = tempfile.mkstemp(
            suffix=".json", dir=_cache_dir(), text=True
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        Path(tmp).replace(p)
    except OSError:
        pass


def load_ark_config() -> tuple[str, str, str]:
    """Return (base_url, api_key, model_id)."""
    k = os.environ.get("OPENCLAW_ARK_API_KEY", "").strip()
    b = os.environ.get("OPENCLAW_ARK_BASE_URL", "").strip()
    m = os.environ.get("OPENCLAW_ARK_MODEL", "ark-code-latest").strip()
    if k and b:
        return b.rstrip("/"), k, m
    cfg_path = os.environ.get("OPENCLAW_CONFIG", "/root/.openclaw/openclaw.json")
    p = Path(cfg_path)
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        prov = data.get("models", {}).get("providers", {}).get("ark", {})
        b2 = (prov.get("baseUrl") or "").strip()
        k2 = (prov.get("apiKey") or "").strip()
        models = prov.get("models") or []
        mid = models[0].get("id", m) if models else m
        if b2 and k2:
            return b2.rstrip("/"), k2, mid
    raise RuntimeError(
        "Ark 未配置：请设置环境变量 OPENCLAW_ARK_BASE_URL 与 OPENCLAW_ARK_API_KEY，"
        "或保证 OPENCLAW_CONFIG 指向的 openclaw.json 含 models.providers.ark。"
    )


def _load_system_prompt() -> str:
    if not _PROMPT_FILE.is_file():
        raise FileNotFoundError(f"缺少 prompt 文件: {_PROMPT_FILE}")
    return _PROMPT_FILE.read_text(encoding="utf-8")


def _parse_json_object(raw: str) -> dict[str, Any]:
    s = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if fence:
        s = fence.group(1).strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            raise ValueError("无法从模型输出中解析 JSON") from None
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("根节点必须是 JSON object")
    return obj


def _validate_profile(obj: dict[str, Any]) -> dict[str, Any]:
    required = (
        "style_name",
        "tone",
        "vocabulary_level",
        "sentence_structure",
        "catchphrases",
        "call_to_action",
        "reference_texts",
    )
    for k in required:
        if k not in obj:
            raise ValueError(f"缺字段: {k}")
    if not isinstance(obj["tone"], list) or not isinstance(
        obj["catchphrases"], list
    ) or not isinstance(obj["reference_texts"], list):
        raise ValueError("tone / catchphrases / reference_texts 须为数组")
    base = {
        "style_name": str(obj["style_name"]).strip()[:200],
        "tone": [str(x).strip() for x in obj["tone"] if str(x).strip()][:12],
        "vocabulary_level": str(obj["vocabulary_level"]).strip()[:200],
        "sentence_structure": str(obj["sentence_structure"]).strip()[:2000],
        "catchphrases": [str(x).strip() for x in obj["catchphrases"] if str(x).strip()][
            :20
        ],
        "call_to_action": str(obj["call_to_action"]).strip()[:2000],
        "reference_texts": [
            str(x).strip() for x in obj["reference_texts"] if str(x).strip()
        ][:5],
    }
    # T5: normalize to archive-driven style fields for deterministic downstream use.
    base["bio"] = str(obj.get("bio") or f"{base['style_name']}创作者，偏{base['vocabulary_level']}表达。").strip()[:400]
    base["ip_positioning"] = str(obj.get("ip_positioning") or base["style_name"]).strip()[:300]
    base["audience"] = str(obj.get("audience") or "关注市场机会与风险拆解的投资者").strip()[:300]
    base["taboo"] = [str(x).strip() for x in (obj.get("taboo") or []) if str(x).strip()][:12]
    base["structure_pref"] = str(obj.get("structure_pref") or base["sentence_structure"]).strip()[:500]
    base["visual_pref"] = str(obj.get("visual_pref") or "偏好关键数据字幕+图表辅助").strip()[:300]
    base["evidence_pref"] = str(obj.get("evidence_pref") or "偏好可追溯数据、快讯锚点与对比论据").strip()[:300]
    return base


def _ark_post(
    b: str, k: str, m: str, data: bytes, system: str, user: str, temperature: float
) -> dict[str, Any]:
    url = f"{b}/chat/completions"
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {k}",
                },
            ),
            timeout=120,
            context=ctx,
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ark HTTP {e.code}: {err_body[:2000]}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Ark 请求失败: {e!s}") from e

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(
            f"Ark 响应无 choices: {json.dumps(payload, ensure_ascii=False)[:1500]}"
        )
    content = choices[0].get("message", {}).get("content") or ""
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("模型返回空 content")
    return {"raw_content": content, "b": b, "k": k, "m": m}


def extract_from_text(transcript: str) -> dict[str, Any]:
    """
    返回 { "profile": { ...7 fields per contract }, "raw_model": str (optional) }
    """
    body = transcript.strip()
    key_hex = _sha256_hex(body.encode("utf-8"))
    if _cache_enabled():
        hit = _read_extract_cache(key_hex)
        if hit is not None:
            return hit

    system = _load_system_prompt()
    user = f"【用户历史文稿开始】\n{body}\n【用户历史文稿结束】\n只输出 JSON。"
    b, k, m = load_ark_config()
    body: dict[str, Any] = {
        "model": m,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    out = _ark_post(b, k, m, data, system, user, 0.2)
    prof = _validate_profile(_parse_json_object(out["raw_content"]))
    raw_slice = out["raw_content"][:8000]
    if _cache_enabled():
        _write_extract_cache(key_hex, prof, raw_slice)
    return {"profile": prof, "raw_model": raw_slice}


def _load_refine_prompt() -> str:
    if not _REFINE_PROMPT_FILE.is_file():
        raise FileNotFoundError(f"缺少 prompt: {_REFINE_PROMPT_FILE}")
    return _REFINE_PROMPT_FILE.read_text(encoding="utf-8")


def refine_from_text(
    existing_style_name: str,
    existing_profile: dict[str, Any],
    existing_reference_texts: list[str],
    new_transcript: str,
) -> dict[str, Any]:
    """
    已有条目的 style_profile + 新样本文，一次性 LLM 合并；返回 { profile, raw_model }。
    existing_profile 不应含 reference_texts 键；若含会忽略 reference_texts。
    """
    prof_copy = {k: v for k, v in existing_profile.items() if k != "reference_texts"}
    block = {
        "style_name": existing_style_name,
        "current_profile": prof_copy,
        "current_reference_excerpts": (existing_reference_texts or [])[:5],
    }
    system = _load_refine_prompt()
    user = (
        "【旧画像与引用片段（JSON）】\n"
        f"{json.dumps(block, ensure_ascii=False, indent=2)}\n\n"
        "【新样本文稿开始】\n"
        f"{new_transcript.strip()}\n"
        "【新样本文稿结束】\n只输出合并后的一个 JSON 对象，键名同初次提取，不要其他文字。"
    )
    refine_key = _sha256_hex(
        f"{_REFINE_CACHE_SCHEMA}\n".encode("utf-8") + user.encode("utf-8")
    )
    if _cache_enabled():
        hit = _read_refine_cache(refine_key)
        if hit is not None:
            return hit

    b, k, m = load_ark_config()
    body: dict[str, Any] = {
        "model": m,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.25,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    out = _ark_post(b, k, m, data, system, user, 0.25)
    prof = _validate_profile(_parse_json_object(out["raw_content"]))
    if not str(prof.get("style_name", "")).strip():
        prof["style_name"] = existing_style_name
    raw_slice = out["raw_content"][:8000]
    if _cache_enabled():
        _write_refine_cache(refine_key, prof, raw_slice)
    return {"profile": prof, "raw_model": raw_slice}
