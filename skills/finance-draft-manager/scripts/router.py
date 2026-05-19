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
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

_ROUTER_SECTORS = ["科技", "新能源", "港股", "黄金", "有色", "银行"]

_ROUTER_SYSTEM_PROMPT = (
    "你是金融资讯Router。菜单已按 [科技, 新能源, 港股, 黄金, 有色, 银行] 分组。"
    "只在各板块自己的候选中选 ID。\n"
    "规则：宁缺毋滥；无强相关就 items=[]；不要为了凑数塞宏观/海外/泛财经。"
    "每板块最多选3条。\n"
    "输出要求：仅输出一个 JSON 对象，不要 Markdown、不要代码围栏、不要任何自然语言说明"
    "（禁止英文如 We selected / Based on / Here is，禁止中文分析过程）。\n"
    "禁止在 JSON 前输出任何字符；全文第一个字符必须是 ASCII 的「{」，最后一个字符必须是「}」。\n"
    "格式：{\"sectors\":{\"科技\":{\"items\":[1,2],\"insight\":\"一句话\"},"
    "\"新能源\":{\"items\":[],\"insight\":\"…\"},\"港股\":{...},\"黄金\":{...},"
    "\"有色\":{...},\"银行\":{...}}}"
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


def _router_retry_extra() -> int:
    try:
        return max(0, min(2, int(os.environ.get("FINANCE_LLM_ROUTER_RETRY_EXTRA", "1"))))
    except ValueError:
        return 1


def _strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _first_balanced_json_object_slice(s: str) -> str | None:
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _parse_llm_json_object(content: str) -> dict[str, Any]:
    """解析模型输出：去围栏、整段 JSON、括号切片（吸收前文推理）、regex 兜底。"""
    content = _strip_code_fences((content or "").strip())
    dec = json.JSONDecoder()
    if content:
        try:
            out = json.loads(content)
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            pass
        try:
            obj, _end = dec.raw_decode(content)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    balanced = _first_balanced_json_object_slice(content)
    if balanced:
        try:
            return json.loads(balanced)
        except json.JSONDecodeError:
            pass
        try:
            obj, _ = dec.raw_decode(balanced)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{[\s\S]*\}", content)
    if m:
        frag = m.group(0)
        try:
            return json.loads(frag)
        except json.JSONDecodeError:
            pass
        try:
            obj, _ = dec.raw_decode(frag)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise RuntimeError(f"router 输出无法解析: {content[:220]}")


def _pick_message_blob(msg: dict[str, Any]) -> str:
    """优先选用像 JSON 的 content；勿把纯推理 reasoning 当正文解析。"""
    content = str(msg.get("content") or "").strip()
    reasoning = str(msg.get("reasoning_content") or "").strip()
    if content.lstrip().startswith("{"):
        return content
    if reasoning.lstrip().startswith("{"):
        return reasoning
    if "{" in content:
        return content
    if "{" in reasoning:
        return reasoning
    return content or reasoning


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
    body = "\n".join(lines)
    return (
        body
        + "\n\n【格式】立即输出且仅输出一个 JSON 对象：全文第一个字符必须是 ASCII 的 { ，"
        "禁止先写 We selected / Based on / 分析过程；键 sectors 下须含六大板块。"
    )


def _call_llm(menu_text: str, base: str, key: str, model: str, timeout: int, *, retry: bool) -> dict[str, Any]:
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
    user_content = menu_text
    if retry:
        user_content += (
            "\n\n【重试】上次输出不合格。请只输出一个 JSON 对象，全文第一个字符必须是 { ，"
            "禁止任何前缀说明；形状："
            '{"sectors":{"科技":{"items":[1],"insight":"…"},'
            '"新能源":{"items":[],"insight":"…"},'
            '"港股":{"items":[],"insight":"…"},'
            '"黄金":{"items":[],"insight":"…"},'
            '"有色":{"items":[],"insight":"…"},'
            '"银行":{"items":[],"insight":"…"}}}'
        )
    payload_obj: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
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
    blob = _pick_message_blob(msg)
    if not blob:
        raise RuntimeError("router content 为空")
    return _parse_llm_json_object(blob)


def _parse_router_output(raw: dict[str, Any]) -> tuple[dict[str, list[int]], dict[str, str]]:
    sectors_raw = raw.get("sectors")
    if not isinstance(sectors_raw, dict) or not sectors_raw:
        sectors_raw = {k: raw[k] for k in _ROUTER_SECTORS if isinstance(raw.get(k), dict)}
    ids_by_sec: dict[str, list[int]] = {}
    insight_by_sec: dict[str, str] = {}
    for sec in _ROUTER_SECTORS:
        sec_data = sectors_raw.get(sec) or {}
        if not isinstance(sec_data, dict):
            sec_data = {}
        items = sec_data.get("items") or []
        ids_by_sec[sec] = [
            int(i) for i in items if isinstance(i, (int, float, str)) and str(i).isdigit()
        ]
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

    for i, c in enumerate(candidates, start=1):
        c["_router_id"] = i

    base, key, model = _load_router_config()
    if not base or not key:
        result.status = "not_configured"
        result.error = "FINANCE_LLM_ROUTER_BASE_URL / API_KEY 未配置"
        return result

    menu_text = _build_menu(candidates)
    timeout = _router_timeout()
    attempts = 1 + _router_retry_extra()
    t0 = time.perf_counter()
    last_err = ""
    for attempt in range(attempts):
        try:
            raw = _call_llm(
                menu_text,
                base=base,
                key=key,
                model=model,
                timeout=timeout,
                retry=(attempt > 0),
            )
            ids_by_sec, insight_by_sec = _parse_router_output(raw)
            result.ids_by_sector = ids_by_sec
            result.insight_by_sector = insight_by_sec
            result.status = "ok" if attempt == 0 else "ok_retry"
            result.error = ""
            break
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc!s}"[:300]
            logger.warning("[router] LLM 调用失败 (%s/%s): %s", attempt + 1, attempts, exc)
            if attempt < attempts - 1:
                try:
                    sleep_sec = max(0.0, float(os.environ.get("FINANCE_LLM_ROUTER_RETRY_SLEEP_SEC", "0.4")))
                except ValueError:
                    sleep_sec = 0.4
                if sleep_sec > 0:
                    time.sleep(sleep_sec)
    else:
        result.status = "failed"
        result.error = last_err

    result.timing_sec = round(time.perf_counter() - t0, 3)
    return result
