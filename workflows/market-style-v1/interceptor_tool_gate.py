"""
网关/调用侧白名单 + 可操作的 [WORKFLOW_DENY] 引导（Reflective Self-Correction）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    raise SystemExit("需要: pip install pyyaml") from None

def load_workflow(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))

def state_def(wf: dict[str, Any], state_id: str) -> dict[str, Any]:
    for st in wf["states"]:
        if st["id"] == state_id:
            return st
    raise KeyError(f"unknown state: {state_id!r}")

def allowed_for_state(wf: dict[str, Any], state_id: str) -> list[str]:
    return list(state_def(wf, state_id).get("allowed_tools") or [])

def gate_tool_call(
    workflow_path: str | Path, *, state_id: str, tool_name: str, wf: dict[str, Any] | None = None
) -> tuple[bool, str | None]:
    """
    返回 (ok, err)。err 为整段可喂给模型作 tool 结果，带「下一步」引导，避免盲目重试。
    """
    wf = wf or load_workflow(workflow_path)
    st = state_def(wf, state_id)
    allow = list(st.get("allowed_tools") or [])
    label = st.get("state_label") or state_id
    hint = (st.get("on_deny_next_steps") or "").strip()
    if "*" in allow or tool_name in allow:
        return True, None
    alist = ", ".join(allow) if allow else "（本步不调用任何工具，仅自然语言）"
    body = (
        f"System [WORKFLOW_DENY]: 当前处于「{label}」阶段，不能使用工具 `{tool_name}`。\n"
        f"**允许的**: [{alist}]。\n"
    )
    if hint:
        body += f"**你应当**: {hint}\n"
    body += f"**不要**: 在不符合阶段时重试同一越权工具；先完成本步或按 workflow 先流转状态。"
    return False, body

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        o, m = gate_tool_call(sys.argv[1], state_id=sys.argv[2], tool_name=sys.argv[3])
        print(json.dumps({"ok": o, "error": m}, ensure_ascii=False))
    else:
        p = Path(__file__).with_name("workflow.yaml")
        print(gate_tool_call(p, state_id="INGEST_MARKET", tool_name="preflight_topic"))
