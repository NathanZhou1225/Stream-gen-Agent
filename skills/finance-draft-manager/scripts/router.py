"""
finance-draft-manager — LLM Router（从 finance-source-ingest/pipeline.py 迁移）。

职责：接收 DB 检索到的候选条目，用轻量级 LLM 按板块分组点菜，
      返回 selected_ids + insight（不生成正文、不补造事实）。

对外入口：run_router(candidates, sectors) -> RouterResult
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib import error as urllib_error
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

_ROUTER_SECTORS = ["科技", "新能源", "港股", "黄金", "有色", "银行"]

_ROUTER_SYSTEM_PROMPT = (
    "你是金融资讯Router。菜单已按 [科技, 新能源, 港股, 黄金, 有色, 银行] 分组。"
    "只在各板块自己的候选中选 ID。\n"
    "规则：宁缺毋滥；无强相关就 items=[]；不要为了凑数塞宏观/海外/泛财经。"
    "每板块最多选3条。\n"
    "CRITICAL：你必须只输出**一个**合法 JSON 对象，不要输出任何自然语言说明（含中文分析）、不要 Markdown、不要代码围栏。\n"
    "格式：{\"sectors\":{\"科技\":{\"items\":[1,2],\"insight\":\"一句话\"},"
    "\"新能源\":{...},\"港股\":{...},\"黄金\":{...},\"有色\":{...},\"银行\":{...}}}"
)


def _use_json_object_env(name: str, *, default_on: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default_on
    return raw in ("1", "true", "yes", "on")


@dataclass
class RouterResult:
    ids_by_sector: dict[str, list[int]] = field(default_factory=dict)
    insight_by_sector: dict[str, str] = field(default_factory=dict)
    status: str = "ok"
    timing_sec: float = 0.0
    error: str = ""


def _chat_completions_url(base: str) -> str:
    """拼出 OpenAI 兼容 `.../v1/chat/completions`；若 base 已以 `/v1` 结尾则不再重复。"""
    b = base.strip().rstrip("/")
    if b.endswith("/v1"):
        return f"{b}/chat/completions"
    return f"{b}/v1/chat/completions"


def _load_router_config() -> tuple[str, str, str]:
    def _env(*names: str) -> str:
        for n in names:
            v = os.environ.get(n, "").strip()
            if v:
                return v
        return ""

    base = _env("FINANCE_LLM_ROUTER_BASE_URL", "OPENCLAW_ARK_BASE_URL", "OPENCLAW_ARK_ENDPOINT")
    key = _env("FINANCE_LLM_ROUTER_API_KEY", "OPENCLAW_ARK_API_KEY", "ARK_API_KEY")
    model = _env("FINANCE_LLM_ROUTER_MODEL", "OPENCLAW_ARK_MODEL", "ARK_MODEL_ID")
    if base and not base.startswith("http"):
        base = f"https://{base}"
    return base.rstrip("/"), key, model


def _router_timeout() -> int:
    try:
        return max(10, int(os.environ.get("FINANCE_LLM_ROUTER_TIMEOUT_SEC", "30")))
    except ValueError:
        return 30


def _build_menu(candidates: list[dict[str, Any]]) -> str:
    """构建按板块分组的简洁菜单文本。"""
    by_sector: dict[str, list[str]] = {s: [] for s in _ROUTER_SECTORS}
    for c in candidates:
        sec = str(c.get("sector") or "")
        if sec not in by_sector:
            sec = "科技"
        title = str(c.get("clean_title") or c.get("raw_title") or "")[:80]
        idx = c.get("_router_id", 0)
        by_sector[sec].append(f"[{idx}] {title}")

    lines: list[str] = []
    for sec in _ROUTER_SECTORS:
        if by_sector[sec]:
            lines.append(f"## {sec}")
            lines.extend(by_sector[sec])
    return "\n".join(lines)


def _call_llm(menu_text: str, base: str, key: str, model: str, timeout: int) -> dict[str, Any]:
    endpoint = _chat_completions_url(base)
    use_json_obj = _use_json_object_env("FINANCE_LLM_ROUTER_JSON_OBJECT", default_on=True)
    try:
        _temp = float(os.environ.get("FINANCE_LLM_ROUTER_TEMPERATURE", "0"))
    except ValueError:
        _temp = 0.0
    try:
        _mt = max(200, int(os.environ.get("FINANCE_LLM_ROUTER_MAX_TOKENS", "1200")))
    except ValueError:
        _mt = 1200
    payload_obj: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": menu_text},
        ],
        "temperature": _temp,
        "max_tokens": _mt,
    }
    if use_json_obj:
        payload_obj["response_format"] = {"type": "json_object"}
    body = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")

    req = urlrequest.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("router 响应无 choices")
    msg = (choices[0] or {}).get("message") or {}
    content = str(msg.get("content") or "").strip()
    if not content:
        content = str(msg.get("reasoning_content") or "").strip()
    if not content:
        raise RuntimeError("router content 为空")

    dec = json.JSONDecoder()

    def _parse_blob(blob: str) -> dict[str, Any]:
        b = (blob or "").strip()
        if not b:
            raise RuntimeError("router content 为空")
        try:
            out = json.loads(b)
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            pass
        try:
            obj, _end = dec.raw_decode(b)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        import re
        m = re.search(r"\{[\s\S]*\}", b)
        if m:
            frag = m.group(0)
            try:
                obj2 = json.loads(frag)
                if isinstance(obj2, dict):
                    return obj2
            except json.JSONDecodeError:
                try:
                    obj3, _ = dec.raw_decode(frag)
                    if isinstance(obj3, dict):
                        return obj3
                except json.JSONDecodeError:
                    pass
        raise RuntimeError(f"router 输出无法解析: {b[:220]}")

    try:
        return _parse_blob(content)
    except RuntimeError:
        reasoning = str(msg.get("reasoning_content") or "").strip()
        if reasoning and reasoning != content:
            return _parse_blob(reasoning)
        raise


def _parse_router_output(raw: dict[str, Any]) -> tuple[dict[str, list[int]], dict[str, str]]:
    sectors_raw = raw.get("sectors") or {}
    ids_by_sec: dict[str, list[int]] = {}
    insight_by_sec: dict[str, str] = {}
    for sec in _ROUTER_SECTORS:
        sec_data = sectors_raw.get(sec) or {}
        items = sec_data.get("items") or []
        ids_by_sec[sec] = [int(i) for i in items if isinstance(i, (int, float, str)) and str(i).isdigit()]
        insight_by_sec[sec] = str(sec_data.get("insight") or "")
    return ids_by_sec, insight_by_sec


def run_router(
    candidates: list[dict[str, Any]],
    *,
    sectors: list[str] | None = None,
) -> RouterResult:
    """
    对 DB 检索到的候选条目执行 LLM Router 点菜。

    :param candidates: news_items 查询结果列表（需含 sector / clean_title / raw_title）
    :param sectors: 过滤板块（None 表示全部）
    :return: RouterResult
    """
    result = RouterResult()
    if not candidates:
        result.status = "no_candidates"
        return result

    # 赋予 _router_id
    for i, c in enumerate(candidates, start=1):
        c["_router_id"] = i

    base, key, model = _load_router_config()
    if not base or not key:
        result.status = "not_configured"
        result.error = "FINANCE_LLM_ROUTER_BASE_URL / API_KEY 未配置"
        return result

    menu_text = _build_menu(candidates)
    timeout = _router_timeout()
    t0 = time.perf_counter()
    try:
        raw = _call_llm(menu_text, base=base, key=key, model=model, timeout=timeout)
        ids_by_sec, insight_by_sec = _parse_router_output(raw)
        result.ids_by_sector = ids_by_sec
        result.insight_by_sector = insight_by_sec
        result.status = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[router] LLM 调用失败: %s", exc)
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc!s}"[:300]
    finally:
        result.timing_sec = round(time.perf_counter() - t0, 3)

    return result
