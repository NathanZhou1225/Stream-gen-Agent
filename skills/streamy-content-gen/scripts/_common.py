"""共用工具：环境变量读取、workspace 路径解析、JSON 输出格式、Draft 相关原子操作。"""

from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


CST = timezone(timedelta(hours=8))

DRAFT_ID_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DRAFT_ID_LEN = 3
DRAFT_ID_MAX_RETRY = 10

STAGE_TOPIC = "topic_picking"
STAGE_OUTLINE = "outline_refining"
STAGE_SCRIPT = "script_refining"
STAGE_FINALIZED = "finalized"
STAGE_DROPPED = "dropped"

VALID_UPDATE_STAGES = {STAGE_TOPIC, STAGE_OUTLINE, STAGE_SCRIPT}

# ---- v0.1.4 常量：阶段序 / data_sources 黑名单 / 不变式文案 ----
# 三段式严格顺序，用于 P0-A 阶段跳跃硬禁跳（forward 方向不能跳，rewind 任意往回）。
STAGE_FORWARD_ORDER = [STAGE_TOPIC, STAGE_OUTLINE, STAGE_SCRIPT]

# P0-B：candidates 落盘时 data_sources 字段的黑名单关键词（命中即视为"脑补/无真实数据"）。
# 匹配规则：大小写不敏感、子串匹配。例如 "LLM brainstorm" 命中 "llm" 和 "brainstorm"。
DATA_SOURCE_BLACKLIST = (
    "brainstorm", "llm", "memory", "脑补", "拍脑袋",
    "no_data", "no-data", "none", "n/a", "null",
)

# P1-E：agent 可读的路径不变式文案（注入 invariants[]，跨会话提醒）。
INVARIANT_DRAFTS_PATH = "drafts/ 永远位于 $WORKSPACE_ROOT/drafts/（不是 skills/**/drafts/）"
INVARIANT_NO_DIRECT_EDIT = (
    "禁止用 edit/write 工具直接改 drafts/**/*.json 或 script.md；"
    "所有结构化改动必须走 draft_manager.py（Cursor hook 物理拦截）"
)
INVARIANT_ARCHIVE_CMD = (
    "列归档稿必须用 `draft_manager.py archive-list --since-days N --json`，"
    "绝对不要用 ls/find 或凭记忆"
)
INVARIANT_NO_STAGE_SKIP = (
    "阶段必须逐级推进：topic_picking → outline_refining → script_refining，"
    "禁止 forward 方向跳阶段（rewind 可任意往回）"
)
INVARIANT_CANDIDATES_DATA_SOURCES = (
    "topic_picking payload 必须带非空的 source_context 或 data_sources，"
    "且至少一条不是 brainstorm/llm/memory/脑补（否则 CANDIDATES_REQUIRE_DATA_SOURCES）"
)
INVARIANT_SET_CHOSEN = (
    "修改 topic_candidates.json.chosen 必须走 "
    "`draft_manager.py update --draft <DID> --set-chosen N`（不要用 edit 工具）"
)


def get_user_id() -> str:
    return os.environ.get("OPENCLAW_USER_ID", "default")


def get_endpoint_id() -> str:
    return os.environ.get("OPENCLAW_ENDPOINT_ID", "default")


def get_workspace_root() -> Path:
    """解析 workspace 根目录，按优先级：
    1. 环境变量 OPENCLAW_WORKSPACE（网关注入时最权威）
    2. 基于脚本自身位置向上回溯，找到含 workspace 标志的目录
       （标志：.openclaw/ 目录 或 AGENTS.md 文件）
    3. 兜底：当前工作目录

    第 2 步是为了挡住 Agent `cd skills/<skill>` 后 cwd 变成 skill 目录
    导致 drafts/ 写到 skill 内部的情况。
    """
    env = os.environ.get("OPENCLAW_WORKSPACE")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve().parent
    for cand in [here, *here.parents]:
        if (cand / "SKILL.md").is_file():
            continue
        if (cand / ".openclaw").is_dir() or (cand / "AGENTS.md").is_file():
            return cand
    return Path.cwd().resolve()


def get_drafts_root() -> Path:
    return get_workspace_root() / "drafts"


def get_active_root() -> Path:
    return get_drafts_root() / "active"


def get_archive_root() -> Path:
    return get_drafts_root() / "archive"


def get_index_path() -> Path:
    return get_drafts_root() / "index.json"


def get_active_draft_dir(user_id: str, draft_id: str) -> Path:
    return get_active_root() / user_id / draft_id


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def today_date_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        emit_error(
            "io",
            "JSON_CORRUPT",
            f"{path} 解析失败：{e}",
            path=str(path),
        )
    return default


def write_json_atomic(path: Path, data: Any) -> None:
    """原子写：先写 .tmp 再 rename，避免写一半崩掉。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")
    tmp.replace(path)


def read_index() -> dict[str, Any]:
    """读取或初始化 index.json。"""
    default = {"version": 1, "users": {}}
    data = read_json(get_index_path(), default=default)
    if not isinstance(data, dict) or "users" not in data:
        return default
    return data


def write_index(index: dict[str, Any]) -> None:
    write_json_atomic(get_index_path(), index)


def ensure_user_entry(index: dict[str, Any], user_id: str) -> dict[str, Any]:
    users = index.setdefault("users", {})
    entry = users.setdefault(
        user_id,
        {"active_drafts": [], "focus": None, "last_activity": now_iso()},
    )
    entry.setdefault("active_drafts", [])
    entry.setdefault("focus", None)
    entry.setdefault("last_activity", now_iso())
    return entry


def gen_draft_id(existing: set[str]) -> str:
    """在给定已存在集合外生成 3 位短 ID，碰撞重试 DRAFT_ID_MAX_RETRY 次。"""
    for _ in range(DRAFT_ID_MAX_RETRY):
        candidate = "".join(random.choice(DRAFT_ID_CHARSET) for _ in range(DRAFT_ID_LEN))
        if candidate not in existing:
            return candidate
    emit_error(
        "draft",
        "ID_COLLISION",
        f"连续 {DRAFT_ID_MAX_RETRY} 次生成的 Draft ID 均冲突，建议清理 active/archive。",
    )
    return ""


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def emit_ok(command: str, result: Any, summary: str | None = None, **extra: Any) -> None:
    payload: dict[str, Any] = {"ok": True, "command": command, "result": result}
    if summary is not None:
        payload["summary"] = summary
    payload.update(extra)
    emit_json(payload)


def emit_error(error_type: str, error_code: str, message: str, **extra: Any) -> None:
    payload = {
        "ok": False,
        "error_type": error_type,
        "error_code": error_code,
        "message": message,
    }
    payload.update(extra)
    emit_json(payload)
    sys.exit(1)


def emit_not_implemented(command: str, hint: str = "") -> None:
    emit_error(
        error_type="skeleton",
        error_code="NOT_IMPLEMENTED",
        message=f"'{command}' 尚未实现（v0.1.0-skeleton）。{hint}".strip(),
    )


# ---------- v0.1.4：跨命令共享的硬约束 ----------

def list_active_draft_ids(user_id: str) -> list[str]:
    """读 index.json 给出 user 当前 active 的所有真实 DID（用于 DID 幻觉兜底提示）。"""
    index = read_json(get_index_path(), default={"users": {}})
    users = index.get("users", {}) if isinstance(index, dict) else {}
    entry = users.get(user_id) or {}
    active = entry.get("active_drafts") or []
    return [did for did in active if isinstance(did, str)]


def assert_draft_in_session(user_id: str, draft_id: str) -> None:
    """P0-D (v0.1.4)：mutating 命令前置校验 `draft_id` 确实在当前 session 的 active 列表里。

    失败 → `DRAFT_NOT_FOUND_IN_SESSION`，附上真实 active_drafts 让 Agent 清楚
    "你记忆里的 #X2E 和磁盘上的 #MP3 不是一回事"。
    """
    active = list_active_draft_ids(user_id)
    if draft_id in active:
        return
    emit_error(
        "draft",
        "DRAFT_NOT_FOUND_IN_SESSION",
        (
            f"Draft #{draft_id} 不在 user={user_id} 的当前 active 列表中。"
            f"当前真实 active: {active or '(空)'}。"
            "Agent 常见错误：从上一轮对话记忆里捞 DID；正确做法是先调 "
            "`draft_manager.py list --json` 拿到真实盘面。"
        ),
        draft_id=draft_id,
        user_id=user_id,
        active_drafts=active,
        hint="如果要新开一条稿，请用 `draft_manager.py create --topic ...`。",
    )


def assert_stage_transition(prev_stage: str, target_stage: str) -> None:
    """P0-A (v0.1.4)：允许 forward 逐级推进、任意 rewind、同阶段 stay。

    明确禁止：
        - topic_picking → script_refining（跳 outline_refining）
        - 任何"跨 2 级" forward 转换

    失败 → `STAGE_SKIP_FORBIDDEN`。
    """
    if prev_stage not in STAGE_FORWARD_ORDER or target_stage not in STAGE_FORWARD_ORDER:
        return  # finalized / dropped 等非 3 段式状态由 caller 自己处理
    i_prev = STAGE_FORWARD_ORDER.index(prev_stage)
    i_target = STAGE_FORWARD_ORDER.index(target_stage)
    if i_target <= i_prev:
        return  # rewind 或 stay 一律允许
    if i_target - i_prev == 1:
        return  # 逐级 forward 推进
    # 跨级 forward = 跳阶段
    expected_next = STAGE_FORWARD_ORDER[i_prev + 1]
    emit_error(
        "stage",
        "STAGE_SKIP_FORBIDDEN",
        (
            f"禁止从 `{prev_stage}` 直接跳到 `{target_stage}`（跨阶段 forward）。"
            f"必须先推进到 `{expected_next}`，然后再推进到 `{target_stage}`。"
        ),
        prev_stage=prev_stage,
        target_stage=target_stage,
        expected_next_stage=expected_next,
        hint=(
            "典型违规：用户说'直接出一条 60 秒视频'，Agent 从 topic_picking 跳 script_refining。"
            "正确流程：create → update topic_picking → update outline_refining → update script_refining，"
            "每一步都让用户确认，这是三段式协同的核心价值。"
        ),
    )


def _flatten_sources(raw: Any) -> list[str]:
    """把 source_context / data_sources / source.data_sources 统一摊平成字符串列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                # 兼容 {"name": "tushare", "endpoint": "..."} 结构
                for key in ("name", "source", "id", "label"):
                    if isinstance(item.get(key), str) and item[key].strip():
                        out.append(item[key].strip())
                        break
        return out
    return []


def _looks_blacklisted(src: str) -> bool:
    s = src.lower().strip()
    return any(bad in s for bad in DATA_SOURCE_BLACKLIST)


def assert_candidates_have_data_sources(payload: dict[str, Any]) -> list[str]:
    """P0-B (v0.1.4)：topic_picking payload 必须带非空、非全黑名单的 data 来源声明。

    接受字段（按优先级合并）：
        1. payload["source_context"]（prompt 文档现用字段）
        2. payload["data_sources"]（兼容同义命名）
        3. payload["source"]["data_sources"]（兼容 outline/script 阶段风格）

    允许的 fallback：若顶层无 source 字段，但所有 candidates[] 都带非黑名单的
    `evidence_anchor` 字符串，也算合格——因为 evidence_anchor 就是 per-candidate
    数据锚点。

    失败 → `CANDIDATES_REQUIRE_DATA_SOURCES`，带 hint 告诉 Agent 怎么补。

    返回 "有效来源"清单（供 invariants / history 记录）。
    """
    collected: list[str] = []
    collected += _flatten_sources(payload.get("source_context"))
    collected += _flatten_sources(payload.get("data_sources"))
    src_obj = payload.get("source")
    if isinstance(src_obj, dict):
        collected += _flatten_sources(src_obj.get("data_sources"))

    non_black = [s for s in collected if not _looks_blacklisted(s)]
    if non_black:
        return non_black

    # 顶层 source 不合格，fallback 到 candidates[].evidence_anchor
    evid_non_black: list[str] = []
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for c in candidates:
            if not isinstance(c, dict):
                continue
            ev = c.get("evidence_anchor")
            if isinstance(ev, str) and ev.strip() and not _looks_blacklisted(ev):
                evid_non_black.append(ev.strip())
    if evid_non_black:
        return evid_non_black

    # 全部不合格，抛错
    emit_error(
        "payload",
        "CANDIDATES_REQUIRE_DATA_SOURCES",
        (
            "topic_picking payload 必须声明非空、非脑补的数据来源："
            "至少在顶层 `source_context[]` / `data_sources[]`，或各 candidates[].evidence_anchor "
            "里给出一条真实来源（如 'tushare:index_daily'、'tophub:weibo'、'新浪财经'）。"
            f"当前收集到 {len(collected)} 条声明，"
            f"全部命中黑名单关键词（{', '.join(DATA_SOURCE_BLACKLIST)}）或为空。"
        ),
        declared_sources=collected,
        blacklist=list(DATA_SOURCE_BLACKLIST),
        hint=(
            "典型修复：在 payload 顶层加 `\"source_context\": [\"tophub:baidu\", "
            "\"tushare:index_daily\"]`，或每个 candidate 的 evidence_anchor 写上真实数据锚点。"
            "时效性主题（'最近'/'今天'/'热点'）应当先调用 "
            "`scripts/fetch_hot_rank.py` 或 `scripts/fetch_market.py`，"
            "然后把返回的 source 字段原样塞进 source_context。"
        ),
    )
    return []  # emit_error 已 sys.exit，这里仅安抚 linter


def compute_invariants(current_stage: str | None, *, event: str) -> list[str]:
    """P1-E (v0.1.4)：根据当前阶段生成 Agent 可读的 invariants[]。

    注入到 emit_ok 的 result 里，任何调用方都能看到。这是"可移植的 L2 约束"——
    即使 Agent 没读 SKILL.md、没看 prompt，拿到 tool result 就会看到这些硬约束。
    """
    invariants = [
        INVARIANT_DRAFTS_PATH,
        INVARIANT_NO_DIRECT_EDIT,
        INVARIANT_NO_STAGE_SKIP,
        INVARIANT_ARCHIVE_CMD,
    ]
    if event == "create" or current_stage == STAGE_TOPIC:
        invariants.append(INVARIANT_CANDIDATES_DATA_SOURCES)
        invariants.append(INVARIANT_SET_CHOSEN)
        invariants.append(
            "next allowed: update --stage topic_picking（带 candidates+source_context）"
            " 或 update --set-chosen N"
        )
    elif current_stage == STAGE_OUTLINE:
        invariants.append(
            "next allowed: update --stage script_refining（完整 script.json），"
            "或 rewind --stage topic_picking 换方向"
        )
    elif current_stage == STAGE_SCRIPT:
        invariants.append(
            "next allowed: finalize（定稿归档） / drop（放弃归档） / "
            "rewind --stage outline_refining 或 topic_picking"
        )
        invariants.append(
            "script.md 由工具从 segments[] 自动渲染；payload 不要传 display_markdown"
        )
    return invariants
