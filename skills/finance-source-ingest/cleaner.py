"""LLM 清洗层（Finance Newsbox 采集后 normalized 字段更新）。

设计约束：
- 只更新 clean_title / clean_summary / sector / sentiment / tags / importance_score。
- 不做选题/观点/稿件生成，不生成 markdown_summary。
- 批量调用，失败不阻断 raw 入库：失败条目标记 llm_clean_status='failed'，raw 数据保留。
- 先接现有网关（OPENCLAW_ARK_* / ARK_API_KEY 等），预留独立清洗模型接口。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urlrequest

from models.item import CleanedFields

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是金融信息清洗助手。对于给定的金融新闻/快讯条目，请输出标准化 JSON，字段如下：
- clean_title: 简洁的规范化标题（去掉来源前缀/日期/噪声）
- clean_summary: ≤80字的核心要点摘要
- sector: 最相关的板块（从以下选择一个或留空）：科技、新能源、港股、黄金、有色、银行、宏观、政策、其他
- sentiment: 情绪倾向（利好/利空/中性）
- importance_score: 重要性评分 0.0~1.0（基于数据确定性、影响范围、时效性）
- tags: 最多5个关键词标签数组

规则：
- 严禁使用"可能/大概/或许/有望/预计"等模糊表达
- 不生成任何观点或投资建议
- 只基于给定原文，不补造信息
- 输出必须是合法 JSON，无多余文字

"""

_CLEAN_SCHEMA = {
    "clean_title": "",
    "clean_summary": "",
    "sector": "",
    "sentiment": "中性",
    "importance_score": 0.0,
    "tags": [],
}


def _load_llm_config() -> tuple[str, str, str]:
    """
    优先读专用清洗配置，回退到通用网关配置。
    返回 (base_url, api_key, model)。
    """
    def _env(*names: str, default: str = "") -> str:
        for n in names:
            v = os.environ.get(n, "").strip()
            if v:
                return v
        return default

    base = _env("FINANCE_INGEST_LLM_CLEAN_BASE_URL", "OPENCLAW_ARK_BASE_URL", "OPENCLAW_ARK_ENDPOINT")
    key = _env("FINANCE_INGEST_LLM_CLEAN_API_KEY", "OPENCLAW_ARK_API_KEY", "ARK_API_KEY")
    model = _env("FINANCE_INGEST_LLM_CLEAN_MODEL", "OPENCLAW_ARK_MODEL")

    # 规范化 base_url（OpenAI 兼容：`.../v1/chat/completions`）
    if base and not base.startswith("http"):
        base = f"https://{base}"
    base = base.rstrip("/")

    return base, key, model


def _chat_completions_url(base: str) -> str:
    """拼出 `/v1/chat/completions`；若 base 已以 `/v1` 结尾则不再重复。"""
    b = (base or "").rstrip("/")
    if not b:
        return ""
    if b.endswith("/v1"):
        return f"{b}/chat/completions"
    return f"{b}/v1/chat/completions"


def _is_enabled() -> bool:
    raw = os.environ.get("FINANCE_INGEST_LLM_CLEAN_ENABLED", "1").strip()
    return raw not in ("0", "false", "FALSE", "no", "NO")


def _timeout_sec() -> int:
    try:
        return max(3, int(os.environ.get("FINANCE_INGEST_LLM_CLEAN_TIMEOUT_SEC", "25")))
    except ValueError:
        return 25


def _batch_size() -> int:
    try:
        return max(1, int(os.environ.get("FINANCE_INGEST_LLM_CLEAN_BATCH_SIZE", "10")))
    except ValueError:
        return 10


def _max_output_tokens() -> int:
    """completion max_tokens；中文+tags 易截断，默认 1024。"""
    try:
        return max(128, int(os.environ.get("FINANCE_INGEST_LLM_CLEAN_MAX_TOKENS", "1024")))
    except ValueError:
        return 1024


def max_clean_rounds_per_run() -> int:
    """
    FINANCE_INGEST_LLM_CLEAN_MAX_ROUNDS_PER_RUN：
    - 0（默认）：不限制批次数，当次 ingest 内一直洗直到无 pending（大批量时注意耗时）。
    - 正整数：每轮 ingest 最多执行这么多「批」（每批最多 BATCH_SIZE 条）。
    """
    try:
        return max(0, int(os.environ.get("FINANCE_INGEST_LLM_CLEAN_MAX_ROUNDS_PER_RUN", "0")))
    except ValueError:
        return 0


def _call_llm(prompt: str, base: str, key: str, model: str, timeout: int) -> dict[str, Any]:
    """单次 LLM 调用，返回解析后的 JSON dict；失败抛出 RuntimeError。"""
    endpoint = _chat_completions_url(base)
    if not endpoint:
        raise RuntimeError("LLM base_url 为空")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": _max_output_tokens(),
        "response_format": {"type": "json_object"},
    }, ensure_ascii=False).encode("utf-8")

    req = urlrequest.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"LLM HTTP {e.code}: {detail}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"LLM 请求失败: {e!s}") from e

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("LLM 响应无 choices")
    content = str(((choices[0] or {}).get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("LLM content 为空")

    # 解析 JSON
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        import re
        m = re.search(r"\{[\s\S]*\}", content)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError(f"LLM 输出无法解析为 JSON: {content[:200]}")


def _build_prompt(item: dict[str, Any]) -> str:
    title = str(item.get("raw_title") or "").strip()
    content = str(item.get("raw_content") or "").strip()[:500]
    source = str(item.get("source") or "")
    return (
        f"来源: {source}\n"
        f"标题: {title}\n"
        f"正文（节选）: {content}"
    )


def _parse_result(raw: dict[str, Any], dedupe_key: str, model: str) -> CleanedFields:
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    score = raw.get("importance_score")
    try:
        score = max(0.0, min(1.0, float(score or 0.0)))
    except (TypeError, ValueError):
        score = 0.0

    return CleanedFields(
        dedupe_key=dedupe_key,
        clean_title=str(raw.get("clean_title") or "")[:200],
        clean_summary=str(raw.get("clean_summary") or "")[:300],
        sector=str(raw.get("sector") or ""),
        sentiment=str(raw.get("sentiment") or "中性"),
        importance_score=score,
        tags=tags[:5],
        llm_clean_model=model,
    )


class LLMCleaner:
    """
    批量 LLM 清洗器。

    用法：
        cleaner = LLMCleaner()
        cleaned, failed_keys = cleaner.clean_batch(pending_items)
    """

    def __init__(self) -> None:
        self._enabled = _is_enabled()
        self._timeout = _timeout_sec()
        self._batch_size = _batch_size()
        base, key, model = _load_llm_config()
        self._base = base
        self._key = key
        self._model = model or "default"

    def is_available(self) -> bool:
        if not self._enabled:
            return False
        if not self._base or not self._key:
            logger.warning(
                "[cleaner] LLM 清洗配置缺失（FINANCE_INGEST_LLM_CLEAN_BASE_URL / API_KEY 均未配置），跳过清洗。"
            )
            return False
        return True

    def clean_batch(
        self,
        items: list[dict[str, Any]],
    ) -> tuple[list[CleanedFields], list[str]]:
        """
        批量清洗。
        返回 (cleaned_fields_list, failed_dedupe_keys)。
        失败条目的 raw 数据已入库，只需调用 storage.mark_clean_failed() 标记。
        """
        if not self.is_available():
            return [], []

        cleaned: list[CleanedFields] = []
        failed_keys: list[str] = []

        for item in items:
            dedupe_key = str(item.get("dedupe_key") or "")
            if not dedupe_key:
                continue
            prompt = _build_prompt(item)
            try:
                raw = _call_llm(
                    prompt,
                    base=self._base,
                    key=self._key,
                    model=self._model,
                    timeout=self._timeout,
                )
                cleaned.append(_parse_result(raw, dedupe_key, self._model))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[cleaner] dedupe_key=%s 清洗失败: %s", dedupe_key, exc)
                failed_keys.append(dedupe_key)

        return cleaned, failed_keys

    @property
    def batch_size(self) -> int:
        return self._batch_size
