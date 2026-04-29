"""Script.md 渲染器（v0.1.3）。

从结构化 `script.json` 的 segments[] 按 §8.4 固定模板生成录制用 script.md。
职责单一：不做数据清洗、不做合规扫描、不做 IO（caller 写盘）。

v0.1.3 设计决策（MEMORY.md 2026-04-22）：
    - Agent 不再直接构造 display_markdown（从 prompt + payload 契约里移除）
    - script.md 一律由本模块渲染，格式跟随 §8.4
    - 未来如需个性化：引入 template_id + structured vars，**不**回退到 Agent 写 markdown
"""

from __future__ import annotations

from typing import Any, Iterable

RENDERER_VERSION = "v0.1.3"


class ScriptRenderError(ValueError):
    """script.json 结构不满足渲染要求。caller 应当转成友好错误返回给 Agent。"""

    def __init__(self, code: str, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def _require(cond: bool, code: str, message: str, *, hint: str = "") -> None:
    if not cond:
        raise ScriptRenderError(code, message, hint=hint)


def _fmt_visual(visual: Any) -> str:
    """把 segment.visual[] 收拢成一行简写，例如 `贴纸:警告 / 配图:柱状图`。"""
    if not visual:
        return ""
    if isinstance(visual, str):
        return visual.strip()
    if isinstance(visual, Iterable):
        parts = [str(v).strip() for v in visual if str(v).strip()]
        return " / ".join(parts)
    return str(visual)


def _fmt_role(role: Any) -> str:
    """role 字段做轻量本地化（保持和现有 script.md 风格一致）。"""
    if not role:
        return "段落"
    mapping = {
        "hook": "Hook",
        "argument_1": "论据 1",
        "argument_2": "论据 2",
        "argument_3": "论据 3",
        "argument": "论据",
        "turn": "转折",
        "scene": "场景",
        "conflict": "冲突",
        "result": "结果",
        "action": "行动启发",
        "cta": "CTA",
    }
    key = str(role).strip().lower()
    return mapping.get(key, str(role))


def _append_appendix(lines: list[str], script_data: dict[str, Any]) -> None:
    appendix = script_data.get("production_appendix")
    if not isinstance(appendix, dict):
        return
    sections = [
        ("镜头建议", "camera_shots"),
        ("贴纸/特效", "stickers_effects"),
        ("配图建议", "visual_assets"),
        ("人物行为", "host_actions"),
    ]
    lines.append("附录｜详细制作指导")
    lines.append("")
    for title, key in sections:
        rows = appendix.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        lines.append(f"【{title}】")
        for row in rows:
            text = str(row).strip()
            if text:
                lines.append(f"- {text}")
        lines.append("")
    adapt = script_data.get("production_style_adaptation")
    if isinstance(adapt, dict):
        ip = str(adapt.get("ip_style_adaptation") or "").strip()
        tone = str(adapt.get("tone_style_adaptation") or "").strip()
        visual = str(adapt.get("visual_style_adaptation") or "").strip()
        if ip or tone or visual:
            lines.append("【风格适配说明】")
            if ip:
                lines.append(f"- IP适配：{ip}")
            if tone:
                lines.append(f"- 语气适配：{tone}")
            if visual:
                lines.append(f"- 视觉适配：{visual}")
            lines.append("")


def render_script_md(script_data: dict[str, Any]) -> str:
    """按 §8.4 固定模板渲染 script.md。

    入参需满足（ScriptRenderError 抛出的常见错误码）：
        - `SCRIPT_SCHEMA_MISSING_FIELD`: 顶层缺 draft_id / duration_sec / segments
        - `SCRIPT_SCHEMA_SEGMENTS_EMPTY`: segments[] 为空
        - `SCRIPT_SCHEMA_SEGMENT_INVALID`: 某段缺 time / role / say
    """
    _require(
        isinstance(script_data, dict),
        "SCRIPT_SCHEMA_NOT_OBJECT",
        "script.json 根节点必须是 JSON object",
    )

    draft_id = script_data.get("draft_id")
    _require(
        isinstance(draft_id, str) and draft_id.strip(),
        "SCRIPT_SCHEMA_MISSING_FIELD",
        "script.json 缺 draft_id",
        hint="Agent 的 payload 必须在 script_refining 阶段传 draft_id 字段",
    )

    duration = script_data.get("duration_sec")
    _require(
        isinstance(duration, (int, float)) and duration > 0,
        "SCRIPT_SCHEMA_MISSING_FIELD",
        "script.json 缺 duration_sec（或非正数）",
        hint="Agent 的 payload 必须带 duration_sec（整数秒）",
    )

    segments = script_data.get("segments")
    _require(
        isinstance(segments, list),
        "SCRIPT_SCHEMA_MISSING_FIELD",
        "script.json 缺 segments[]（必须是数组）",
    )
    _require(
        len(segments) > 0,
        "SCRIPT_SCHEMA_SEGMENTS_EMPTY",
        "script.json.segments[] 为空，无法渲染",
    )

    lines: list[str] = []
    lines.append(f"──── 逐字稿 #{draft_id}（约 {int(duration)} 秒）────")
    lines.append("")

    for i, seg in enumerate(segments):
        _require(
            isinstance(seg, dict),
            "SCRIPT_SCHEMA_SEGMENT_INVALID",
            f"segments[{i}] 不是 JSON object",
        )
        time = seg.get("time")
        role = seg.get("role")
        say = seg.get("say")
        _require(
            isinstance(time, str) and time.strip(),
            "SCRIPT_SCHEMA_SEGMENT_INVALID",
            f"segments[{i}] 缺 time（格式 M:SS-M:SS）",
        )
        _require(
            isinstance(role, str) and role.strip(),
            "SCRIPT_SCHEMA_SEGMENT_INVALID",
            f"segments[{i}] 缺 role",
        )
        _require(
            isinstance(say, str) and say.strip(),
            "SCRIPT_SCHEMA_SEGMENT_INVALID",
            f"segments[{i}] 缺 say（口播文案）",
        )

        visual_str = _fmt_visual(seg.get("visual"))
        role_label = _fmt_role(role)
        head_parts = [time.strip(), "·", role_label]
        if visual_str:
            head_parts.extend(["·", visual_str])
        head = "[" + " ".join(head_parts) + "]"

        lines.append(head)
        lines.append(say.strip())
        lines.append("")

    _append_appendix(lines, script_data)
    lines.append("────")
    lines.append("修改还是定稿？")

    return "\n".join(lines) + "\n"


__all__ = ["render_script_md", "ScriptRenderError", "RENDERER_VERSION"]
