#!/usr/bin/env python3
"""从 workflow.yaml + session JSON 生成本步短 SOP。支持 max_retries / fallback。依赖: pyyaml"""
import json, sys, yaml
from pathlib import Path

def main() -> None:
    wf = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
    s = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8") or "{}")
    by = {x["id"]: x for x in wf["states"]}
    cur = s.get("workflow_state", "INITIATE")
    if cur not in by:
        print(f"## ⚠ 未知 `workflow_state`: `{cur}`，已回退 INITIATE\n")
        cur = "INITIATE"
    c = by[cur]
    miss = [k for k in c.get("required_context_keys") or [] if s.get(k) in (None, "", [])]
    at = ", ".join(f"`{t}`" for t in c.get("allowed_tools") or []) or "*(无 — 仅对话)*"
    need = ", ".join(c.get("required_context_keys") or []) or "—"
    nxt = f"`{c['next_state']}`" if c.get("next_state") is not None else "*(终态)*"
    mr, fb = c.get("max_retries"), c.get("fallback_state")
    rc = int(s.get("step_retry_count", 0) or 0)
    lines = [
        f"## 当前: `{cur}`（{c.get('state_label', cur)}）\n", c.get("description", ""), "", "- **本步仅允许** " + at,
        f"- **集齐可流转** [{need}]\n- **下一状态** {nxt}", "",
    ]
    ote, oten = c.get("on_tool_error_state"), c.get("on_tool_error_note")
    if ote:
        lines += [f"- **若工具致命失败/超时**: 将 `workflow_state` → `{ote}`" + (f"（{oten}）" if oten else ""), ""]
    if miss and mr is not None and rc >= mr and fb:
        lines += [
            "### ⛔ 已达本步 `max_retries`，建议降级（防死循环）\n",
            f"- 将 `workflow_state` 设为 `{fb}`，并将 `step_retry_count` 置 **0**（其余字段按业务是否清空）\n",
        ]
    elif miss and mr is not None:
        lines += [f"- **本步重试**: `step_retry_count`={rc} / `max_retries`={mr}；未齐: {', '.join(miss)}", ""]
    elif miss:
        lines += [f"- **未齐**: {', '.join(miss)}", ""]
    else:
        lines += ["- **上下文字段**: OK", ""]
    print("\n".join(lines).rstrip() + "\n")
if __name__ == "__main__":  # noqa: E701
    main()
