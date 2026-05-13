# MEMORY 碎片 · 开稿与 Workflow（按需读取）

> 触发：用户要**开稿、选题、大纲、逐字稿、定稿**，或涉及 **`draft_manager` / `preflight` / `style_id`**。**若本轮只做行情快照，可读 `MEMORY_ingest.md` 即可，不必读本文件。**
>
> **口语意图 / 路由**：先读 `skills/streamy-content-gen/prompts/natural-language-intent.md`（短索引），再读 `prompts/fragments/intent-core.md`；长表与历史话术按需读 `intent-examples.md`（与 `PROMPT_SLIMMING_DESIGN_v0.1.md` Phase 3 一致）。

---

## 五、核心规则 - Workflow 流程规范

### 5.0 开稿前置流程（必须严格执行 · 硬门禁）
每次内容生成必须严格按照以下顺序执行，**不得跳步、不得遗漏。每一步完成后必须打勾确认后再进入下一步**：

✅ **门禁0：user-style 绑定检查清单（进入 `outline_refining` / 大纲 JSON 落盘前必须完成；选题与方向证据包阶段不要求已绑 `style_id`，与 `draft_manager` v0.2.1 一致）**
- [ ] 已调用 `user_style_manager list` 拉取当前所有可用风格
- [ ] 已向用户展示所有可用风格并明确询问选择哪一个
- [ ] 已收到用户明确的 user-style 选择答复（不得用“默认风格/通用风格”跳过）
- [ ] 已将选定的 `style_id` 绑定到对应 draft

✅ **步骤1**：若用户仅要盘面/信源事实，按 **§4.1**（见 `MEMORY_ingest.md`）跑 **全量 `ingest`** 并原样展示 `markdown_summary`；若用户已进入**开稿**链，则按 `streamy-content-gen` / `preflight` 拉取信息并生成 `topic_payload`，**不要**用旧五段式替代 ingest 输出。
✅ **步骤2（选题候选）**：将 `topic_payload` 落入 `draft_manager update --stage topic_picking`，展示三候选；每个候选必须有标题、核心论点、3 条论据。此时只让用户选 1/2/3，不进入风格或大纲。
✅ **步骤3（方向证据包闸）**：用户选择候选后，先 `draft_manager update --set-chosen <N>`，再用 `preflight_topic.py --candidate-id <N> --topic-payload-file <上轮 topic_payload.json> --snapshot-path <上轮 snapshot_path> --allow-targeted-fetch` 生成该候选方向的 `evidence_pack`，并用 `draft_manager update --set-evidence-pack-file <evidence_pack.json>` 落盘。证据包必须围绕已选方向，例如选「油价冲击验证时刻」就只补该方向的事实、数据、来源和论据补强点，不得展示无关 D1/D2 单条详情。
✅ **步骤4**：证据包展示完成且用户确认继续后，**必须执行门禁0拉取风格列表并询问用户选择，禁止直接进入大纲**
✅ **步骤5**：确认风格并完成 draft 的 `style_id` 绑定后，再进入正式的「大纲→逐字稿→定稿」流程

✅ **步骤5B（大纲 prompt · 与 PROMPT_SLIMMING Phase 2 对齐）**：生成大纲前默认读取 `prompts/outline-generation.md`（短索引），并随之读取 `prompts/fragments/outline-core.md` 与 `prompts/fragments/outline-min-schema.md`；结构不稳或用户要样例时再读 `outline-examples.md`。

✅ **步骤6（生成 payload 前预校验 · 降耗硬规则）**：大纲与逐字稿 payload 不得先写空壳再逐步补字段。生成后必须先用 `draft_manager.py update --stage outline_refining|script_refining --payload-file <json> --validate-only --json` 做 dry-run；若返回 `errors[]`，一次性修复全部错误后再正式 update。

✅ **步骤7（逐字稿最小 schema · 不降质瘦身）**：逐字稿阶段默认只读 `prompts/fragments/script-core.md` 与 `prompts/fragments/script-min-schema.md`。payload 只带必要字段；不得主动传 `stage`、`style_id`、`compliance`、`display_markdown`，`production_appendix` 只保留 `camera_shots` / `stickers_effects` / `visual_assets` / `host_actions` 四块。禁止用统一 `role: "host"` 或不传 `user_style_context` 来绕过事实证据与风格门禁。

### 5.0A 明确方向优先（新增硬规则，优先级高于 5.0）
- 当用户首句或当前回合已明确给出**开稿方向**（例如“为我AI行情方面进行开稿”），**禁止**先走“通用热点兜圈子”再二次确认方向。
- 必须直接按该方向执行开稿链路：`preflight_topic.py --direction "<用户原话方向>" -> topic_picking 三候选 -> 用户选候选 -> 方向证据包 -> 风格选择 -> outline_refining`。证据包展示完成并确认继续后，才允许进入风格门禁。
- 用户方向词必须原样进入 `topic_payload.direction`，且候选标题/论点必须与该方向语义相关；若相关性不足，必须当轮重生候选，不得直接展示。
- 用户表达「先看盘 / 只要事实 / 不开稿」时：**盘面事实**仍以 **`MEMORY_ingest.md` §4.1** 全量 `ingest` → `markdown_summary` 原样** 为准；**禁止**用旧五段式替代或删减 ingest 块。旧五段式仅用于 `MEMORY_ingest.md` §4.1 第 4 点所列的**手写解读/复盘**场景。

### 5.0C 安全批量 helper（只覆盖非用户决策段）
- 可用 `scripts/stream_gen_workflow_helper.py start-topic --direction "<方向>"` 打包执行：创建 Draft → preflight → topic_picking 落盘；返回三候选后必须停下来等用户选 1/2/3。
- 用户选定后，可用 `scripts/stream_gen_workflow_helper.py apply-choice --draft <DID> --candidate-id <N> --topic-payload-file <file> --snapshot-path <snapshot>` 打包执行：`set-chosen` → 生成方向证据包 → 落盘证据包；返回后必须展示证据包并等用户确认进入 user-style。
- 可用 `scripts/stream_gen_workflow_helper.py validate-script --draft <DID> --payload-file <script.json>` 包装逐字稿 `--validate-only`。
- 禁止写一个从创建到定稿全自动跑完的脚本；候选选择、证据包确认、风格选择、定稿/auto-refine 仍是用户决策节点。

### 5.0B 大纲生成前置校验（生成大纲前必须执行）
- [ ] 检查当前 draft 是否已绑定 `style_id`，未绑定则不得生成大纲，必须先确认风格
- [ ] 已读取对应风格的 `style_context`，明确该风格的结构偏好、语气偏好、禁忌要求
- [ ] 大纲结构必须符合对应风格的 `structure_pref` 要求，不得使用通用模板
- [ ] 老丁财经风格必须采用「方法论+信号拆解+盘面提醒」三段式，先给明确结论，再讲逻辑，最后给操作建议，禁止使用空泛宏观分析结构

### 5.1 稿件追踪与标识
- 在展示选题方向、成稿、状态更新等内容时，必须明确标注当前处理的草稿ID（如 **#QH3**），方便用户追踪和管理多篇稿件
- 稿件ID应在每次重要操作前醒目显示

### 5.2 成稿收尾流程
- 成稿完成后，必须主动询问用户两个问题：
  1. 是否确认定稿并归档此稿件？
  2. 如果确认归档，是否需要自动执行 auto-refine 来优化绑定的风格？
- 这是workflow收尾阶段的必要步骤，不得遗漏

### 5.3 异常处理机制
- 当执行 `--set-chosen` 出现 `TOPIC_CANDIDATES_EMPTY` 错误时，说明 `topic_candidates.json` 缺少 `candidates` 字段
- 处理方式：手动构建包含 `candidates` 数组的正确格式JSON文件，包含至少3个候选选题，并写入后重试

### 5.4 选题展示硬规则（飞书）
- 在 `topic_picking` 阶段，展示候选时**禁止**只给“选题标题 + 切入角度”
- 每个候选**必须同时展示**：`标题` + `核心论点(thesis)` + `3条论据(evidence)`
- 若本轮漏展示论点/论据，必须按同一 Draft 立即补发完整候选卡片
- 论据必须与用户方向同域（例如“AI行情”方向下，论据不可全部是无关宏观泛新闻）；若不相关，先重生候选再展示
