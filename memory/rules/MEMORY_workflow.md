# MEMORY 碎片 · 开稿 Workflow 索引（省 Token）

> **触发**：用户要开稿、选题、大纲、逐字稿、定稿，或涉及 `draft_manager` / `preflight` / `style_id` / `content_type`。**仅做行情快照时读 `MEMORY_ingest.md` 即可，不必读本文件。**

---

## 场景分流（伪 RAG）

| 阶段 | 指针 |
|------|------|
| **进入开稿链** | 本文件（索引）→ 按需读 `workflow_gate.md` |
| **选题候选展示** | `workflow_gate.md §5.4`（标题 + thesis + 3 论据） |
| **证据包落盘** | `workflow_gate.md §5.0 步骤3`（--apply-topic-choice） |
| **风格绑定** | `workflow_gate.md §5.0 门禁0`（user-style list + bind） |
| **大纲生成** | `workflow_gate.md §5.0B` + `prompts/outline-core.md` |
| **逐字稿生成** | `workflow_gate.md §5.6` + `MEMORY_script_templates.md` + `prompts/script-core.md` |
| **定稿归档** | `workflow_gate.md §5.2`（询问 + auto-refine） |

---

## 极简门禁（P0，必须遵守）

1. **三段式门禁**：topic_picking → outline_refining → script_refining → finalized，不得跳步
2. **越级禁令**：未落盘前不得在聊天里输出大纲/逐字稿正文
3. **证据包必落盘**：用户选候选后必须先展示并落盘 evidence_pack，不得直接进风格/大纲
4. **风格必绑定**：进 outline 前必须 user-style list + 用户选择 + bind style_id
5. **制作附录必完整**：逐字稿 `production_appendix` 四块（camera_shots / stickers_effects / visual_assets / host_actions）每块 3-5 条
6. **飞书去工程化**：禁止直播内部步骤、贴 CLI 报错；单条消息交付里程碑产物

---

## 口语意图路由

- 用户说"开稿 / 要选题 / 要大纲 / 要逐字稿" → 进入开稿链（见上表）
- 用户说"先看盘 / 只要事实 / 拉行情" → `MEMORY_ingest.md` 全量 ingest + `markdown_summary` 原样
- 用户说"明确方向"（如"AI行情开稿"） → 直接 preflight --direction → topic_picking（见 `workflow_gate.md §5.0A`）
- 用户说"XX方向有什么信息"（未要全量表） → `query_direction_brief.py --direction`（见 `MEMORY_ingest.md` §4.1）

---

## CLI 快速参考

```bash
# 创建 + preflight + topic_picking（批量）
python3 scripts/stream_gen_workflow_helper.py start-topic --direction '<方向>'

# 证据包落盘（一次写入 chosen + evidence_pack）
python3 scripts/draft_manager.py update --draft <DID> --apply-topic-choice <N>

# 风格绑定
python3 scripts/draft_manager.py update --draft <DID> --set-style-id <UUID>

# 稿件类型 + IP
python3 scripts/draft_manager.py update --draft <DID> --set-content-type <type> [--set-ip-id <stem>]

# 大纲/逐字稿预检
python3 scripts/draft_manager.py update --stage outline_refining|script_refining --payload-file <json> --validate-only --json

# 定稿归档
python3 scripts/draft_manager.py finalize --draft <DID> --auto-refine --min-context-reset
```

---

> **降耗提示**：本文件约 1.8KB（索引页）；详细门禁规则约 2.9KB 在 `workflow_gate.md`，按需 read。预计每轮减少 **2000-3000 Token** 规则读取。