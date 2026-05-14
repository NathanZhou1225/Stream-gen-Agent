"""
finance-draft-manager — 板块小 LLM 润色（从 finance-source-ingest/pipeline.py 迁移）。

职责：对 Router 选出的条目，按板块并发调用轻量 LLM 重写展示文本
     （板块洞察 / 事件 / 影响 / 角度），不补造事实、不改写数值。

对外入口：rewrite_sectors(items_by_sector) -> RewriteResult
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any
from urllib import error as urllib_error
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

_SAFETY_BOUNDARY = (
    "你是金融内容编辑。基于给定板块的已选事实，重写展示文本。\n"
    "死命令：严禁使用模糊性表达（可能/大概/或许/有望/预计/也许/或将）！\n"
    "只重写展示文案，不补造事实、不改写数字、不引入菜单外信息。\n"
    "输出要求：仅输出一个 JSON 对象，不要 Markdown、不要解释性文字、不要用代码围栏；\n"
    "键名严格为 insight（字符串）与 items（数组，元素含 title、impact、angle、sentiment 字符串）。\n"
    "sentiment 取值仅限：利好、利空、中性；必须与 title、impact、angle 对读者传达的**结论方向一致**"
    "（若宏观数据偏空但本条写的是该板块/标的受益逻辑，须标「利好」而非「利空」，禁止前缀与正文自相矛盾）。\n"
    "insight 须为完整中文句（不少于 12 字），禁止使用「…」「...」占位或空话。\n"
    "禁止在 JSON 前输出任何字符（含「我们」「需要」「首先」）；全文第一个字符必须是 ASCII 的「{」。\n"
    "示例形状：{\"insight\":\"……\",\"items\":[{\"title\":\"……\",\"impact\":\"……\",\"angle\":\"……\",\"sentiment\":\"利好\"}]}"
)


def _use_json_object_env(name: str, *, default_on: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default_on
    return raw in ("1", "true", "yes", "on")


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
    """自首个 ASCII `{` 起括号深度配对，得到最外层 JSON 对象（字符串内括号不计）。"""
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
    """解析模型输出：去围栏、整段 JSON、括号切片（吸收前文推理）、贪婪 regex 兜底。"""
    content = _strip_code_fences((content or "").strip())
    dec = json.JSONDecoder()
    if content:
        try:
            return json.loads(content)
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
    raise RuntimeError(f"rewriter 输出无法解析: {content[:220]}")


@dataclass
class SectorRewriteResult:
    sector: str
    insight: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    timing_sec: float = 0.0
    error: str = ""


@dataclass
class RewriteResult:
    by_sector: dict[str, SectorRewriteResult] = field(default_factory=dict)
    total_timing_sec: float = 0.0


def _chat_completions_url(base: str) -> str:
    b = base.strip().rstrip("/")
    if b.endswith("/v1"):
        return f"{b}/chat/completions"
    return f"{b}/v1/chat/completions"


def _is_enabled() -> bool:
    raw = os.environ.get("FINANCE_SECTOR_LLM_REWRITE_ENABLED", "0").strip()
    return raw in ("1", "true", "TRUE", "yes", "YES")


def _timeout() -> int:
    try:
        return max(5, int(os.environ.get("FINANCE_SECTOR_LLM_REWRITE_TIMEOUT_SEC", "25")))
    except ValueError:
        return 25


def _load_config() -> tuple[str, str, str]:
    def _env(*names: str) -> str:
        for n in names:
            v = os.environ.get(n, "").strip()
            if v:
                return v
        return ""

    # 与 Router / ingest cleaner 共用 DeepSeek 等 OpenAI 兼容网关时可只配一组变量
    base = _env(
        "FINANCE_SECTOR_LLM_BASE_URL",
        "FINANCE_LLM_ROUTER_BASE_URL",
        "FINANCE_INGEST_LLM_CLEAN_BASE_URL",
        "OPENCLAW_ARK_BASE_URL",
        "OPENCLAW_ARK_ENDPOINT",
    )
    key = _env(
        "FINANCE_SECTOR_LLM_API_KEY",
        "FINANCE_LLM_ROUTER_API_KEY",
        "FINANCE_INGEST_LLM_CLEAN_API_KEY",
        "OPENCLAW_ARK_API_KEY",
        "ARK_API_KEY",
    )
    model = _env(
        "FINANCE_SECTOR_LLM_MODEL",
        "FINANCE_LLM_ROUTER_MODEL",
        "FINANCE_INGEST_LLM_CLEAN_MODEL",
        "OPENCLAW_ARK_MODEL",
    )
    if base and not base.startswith("http"):
        base = f"https://{base}"
    return base.rstrip("/"), key, model


def _call_llm(prompt: str, base: str, key: str, model: str, timeout: int) -> dict[str, Any]:
    endpoint = _chat_completions_url(base)
    use_json_obj = _use_json_object_env("FINANCE_SECTOR_LLM_JSON_OBJECT", default_on=True)
    try:
        _max_tok = max(300, int(os.environ.get("FINANCE_SECTOR_LLM_MAX_TOKENS", "640")))
    except ValueError:
        _max_tok = 640
    payload_obj: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SAFETY_BOUNDARY},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": _max_tok,
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
    msg = (choices[0] or {}).get("message") or {} if choices else {}
    content = str(msg.get("content") or "").strip()
    if not content:
        content = str(msg.get("reasoning_content") or "").strip()
    if not content:
        raise RuntimeError("rewriter content 为空")

    try:
        return _parse_llm_json_object(content)
    except RuntimeError:
        reasoning = str(msg.get("reasoning_content") or "").strip()
        if reasoning and reasoning != content:
            return _parse_llm_json_object(reasoning)
        raise


def _hint_to_sent_label(hint: str) -> str:
    h = (hint or "").strip() or "中性"
    if "利空" in h:
        return "利空"
    if "利好" in h:
        return "利好"
    return "中性"


def _normalize_item_sentiment(raw: Any, fallback_hint: str) -> str:
    s = str(raw or "").strip()
    if s in ("利好", "利空", "中性"):
        return s
    if "利空" in s:
        return "利空"
    if "利好" in s:
        return "利好"
    if "中性" in s:
        return "中性"
    return _hint_to_sent_label(fallback_hint)


def _build_sector_prompt(sector: str, items: list[dict[str, Any]]) -> str:
    lines = [f"板块：{sector}", "已选事实（与 items 顺序一致，至多 3 条）："]
    for it in items[:3]:
        title = str(it.get("clean_title") or it.get("raw_title") or "")
        summary = str(it.get("clean_summary") or "")[:120]
        lines.append(f"- {title}：{summary}" if summary else f"- {title}")
    return (
        "\n".join(lines)
        + "\n\n【格式】立即输出且仅输出一个 JSON 对象：全文第一个字符必须是 ASCII 的 { ，"
        "禁止先写分析过程；items 长度与上列表格条数一致、不超过 3。"
    )


def _rewrite_sector(
    sector: str,
    items: list[dict[str, Any]],
    base: str,
    key: str,
    model: str,
    timeout: int,
) -> SectorRewriteResult:
    res = SectorRewriteResult(sector=sector)
    t0 = time.perf_counter()
    try:
        extra = int(os.environ.get("FINANCE_SECTOR_LLM_REWRITE_RETRY_EXTRA", "1"))
    except ValueError:
        extra = 1
    extra = max(0, min(2, extra))
    attempts = 1 + extra
    try:
        retry_sleep = max(0.0, float(os.environ.get("FINANCE_SECTOR_LLM_REWRITE_RETRY_SLEEP_SEC", "0.4")))
    except ValueError:
        retry_sleep = 0.4

    last_err = ""
    succeeded = False
    for attempt in range(attempts):
        try:
            prompt = _build_sector_prompt(sector, items)
            if attempt > 0:
                prompt += (
                    "\n\n【重试】上次输出不合格。请只输出一个 JSON 对象，全文第一个字符必须是 { ，"
                    "禁止任何前缀说明；形状："
                    "{\"insight\":\"……\",\"items\":[{\"title\":\"\",\"impact\":\"\",\"angle\":\"\",\"sentiment\":\"利好\"}]}。"
                )
            raw = _call_llm(prompt, base=base, key=key, model=model, timeout=timeout)
            res.insight = str(raw.get("insight") or "")
            raw_items = raw.get("items") or []
            if isinstance(raw_items, list):
                built: list[dict[str, Any]] = []
                for j, i in enumerate(raw_items):
                    if not isinstance(i, dict) or len(built) >= 3:
                        continue
                    src = items[j] if j < len(items) else {}
                    fh = str((src or {}).get("sentiment_hint") or "中性")
                    built.append({
                        "title": str(i.get("title") or "")[:100],
                        "impact": str(i.get("impact") or "")[:120],
                        "angle": str(i.get("angle") or "")[:120],
                        "sentiment": _normalize_item_sentiment(i.get("sentiment"), fh),
                    })
                res.items = built
            res.status = "ok"
            res.error = ""
            succeeded = True
            break
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc!s}"[:200]
            logger.warning(
                "[rewriter] %s 润色失败 (%s/%s): %s",
                sector,
                attempt + 1,
                attempts,
                exc,
            )
            if attempt < attempts - 1 and retry_sleep > 0:
                time.sleep(retry_sleep)

    if not succeeded:
        res.status = "failed"
        res.error = last_err

    res.timing_sec = round(time.perf_counter() - t0, 3)
    return res


def rewrite_sectors(
    items_by_sector: dict[str, list[dict[str, Any]]],
) -> RewriteResult:
    """
    对各板块候选条目并发调用小 LLM 润色。

    :param items_by_sector: {板块名: [news_item, ...]}
    :return: RewriteResult（失败板块 status='failed'，不影响其他板块）
    """
    result = RewriteResult()

    if not _is_enabled():
        for sec in items_by_sector:
            result.by_sector[sec] = SectorRewriteResult(sector=sec, status="disabled")
        return result

    base, key, model = _load_config()
    if not base or not key:
        for sec in items_by_sector:
            result.by_sector[sec] = SectorRewriteResult(sector=sec, status="not_configured")
        return result

    timeout = _timeout()
    t0 = time.perf_counter()

    try:
        max_workers = max(1, min(6, int(os.environ.get("FINANCE_SECTOR_LLM_REWRITE_MAX_WORKERS", "2"))))
    except ValueError:
        max_workers = 2
    sectors = [s for s, items in items_by_sector.items() if items]
    with ThreadPoolExecutor(max_workers=min(len(sectors), max_workers)) as pool:
        futures = {
            pool.submit(_rewrite_sector, sec, items_by_sector[sec], base, key, model, timeout): sec
            for sec in sectors
        }
        for fut in as_completed(futures):
            sec_result = fut.result()
            result.by_sector[sec_result.sector] = sec_result

    result.total_timing_sec = round(time.perf_counter() - t0, 3)
    return result
