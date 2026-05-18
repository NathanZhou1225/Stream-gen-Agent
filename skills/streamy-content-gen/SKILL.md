---
name: streamy-content-gen
description: |
  Streamy 短视频/口播：Draft 多阶段（topic_picking → outline_refining → script_refining）与合规闸；支持 finance-source-ingest 事实管道。
  带明确方向开稿：须先 preflight_topic.py；先进入 topic_picking 展示候选，用户选定候选后必须生成该方向 evidence_pack，再进入 user-style 与大纲；候选每条必须含标题+核心论点+3条论据；draft_id 须为 draft_manager 三字符 + active/default 目录；禁止同轮越级输出大纲/逐字稿/分镜成稿。
---

# streamy-content-gen

## 三段式门禁（P0，硬约束）

飞书/对话里常见失效模式是：**事实拉取成功 → 模型直接在聊天里写「视频大纲」「逐字稿」**，从而绕过 `drafts/` 落盘与审计链。本技能 **禁止** 该行为。

1. **阶段名（与 `drafts/.../history.json` 对齐）**  
   `topic_picking` → **`outline_refining`** → **`script_refining`** →（合规扫描等）→ `finalized`。不得自创阶段名、不得跳步。

2. **越级成稿禁令**  
   只要用户请求属于 **带方向开稿 / 要选题 / 要大纲 / 要口播 / 要逐字稿**（含「直接开稿」话术），在未通过 `draft_manager` 将当前稿推进到对应 `stage` 并已落盘前，**禁止**在助手回复中输出以下形态的**正文**（摘要级状态行除外）：  
   - 带时间轴/分段的 **视频大纲**（如「【开头 0–15 秒】…」整段展开）；  
   - **逐字稿**、口播成稿、**`[0–5s]`** 类分镜全文。  
   **唯一例外**：用户明确只要「闲聊讲解、不要落盘、不要 draft」，且未触发本技能 Playbook。

3. **单次回复边界（带方向开稿路径）**  
   执行完 `preflight_topic.py` 后，允许将返回的 **`topic_payload`** 作为唯一 JSON 体执行 **`draft_manager update --stage topic_picking` 一次**，并展示候选（每条含标题 + 核心论点 + 3 条论据）+ 选号指令；**不得**同轮继续写大纲/逐字稿。  
   **证据包已合并入 preflight**：`preflight_topic.py --direction` **一次**产出 `candidates[]` 与 **`candidate_evidence_packs`**（键 `"1"`/`"2"`/`"3"`，预计算）。用户选定 1/2/3 后：  
   - **优先** `draft_manager update --draft <DID> --apply-topic-choice <N>`（一次写入 `chosen` + `candidate_evidence_pack.json`，读内嵌包，**无需**二次 preflight、**无需**手写临时 evidence JSON）；  
   - 或 `scripts/stream_gen_workflow_helper.py apply-choice --draft <DID> --candidate-id <N>`（同上；旧稿无内嵌包时 helper 自动 legacy 回退，可用 `--force-preflight`）；  
   - 只向用户展示该候选的 `evidence_pack`（核心事实、详细来源、论据补强点、缺口）；用户确认后才进入稿件类型与 user-style。  
  **唯一例外（纯拉数 / 非 topic_picking）**：用户仅要「今日行情 / 热点 / 全量 / 信源快照」**任一口径**且**未**进入带方向开稿链时，按 **`prompts/fragments/intent-core.md` §4.4**（经 `natural-language-intent.md` 索引引入）：必须执行 **`python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --max-items 30 --summary-only`**，并将返回 JSON 的 **`markdown_summary` 全文原样** 展示（不得拆成只行情或只热点、不得拆两条消息、不得自行重排版删段）。  
   **纯拉数**：**只**用 `query_market_facts.py`（云端 API，约 1～2 分钟）；**禁止** `ingest.py run` / 本地 SQLite。**不**拼接 Tavily。仅用户明确要求实时刷新时用 `--live-fetch`（legacy，更慢）。  
   **禁止**仅用三条候选标题复述用户原话却 **不展示** 任何 ingest 事实锚点（用户会误以为未拉信源）。  
   **同一轮内不得**再调用模型写 `outline_refining` / `script_refining` 内容，也不得在聊天里预写大纲/逐字稿「代替」落盘。

4. **何时才能写大纲 / 逐字稿**  
   - **大纲**：仅当 `topic_picking` 已完成 **选题确认**（如 `set_chosen` 或 Playbook 规定的等价动作）之后，执行 **`draft_manager update --stage outline_refining`**。  
   - **逐字稿**：仅当 `outline_refining` 已落盘且用户/流程允许进入口播阶段后，执行 **`draft_manager update --stage script_refining`**。
   - **回复收敛（新增）**：一旦进入 `outline_refining` 或 `script_refining`，默认**不再**重复输出「信源状态 / 大盘行情 / 市场焦点 / 事实依据」整块。除非用户明确要求「再看快讯/再看数据来源」，否则只回复当前阶段产物（大纲或逐字稿）与必要确认指令。

## 带方向开稿（契约 · P0）

当用户提供 **明确的选题方向** 并要求 **直接开稿 / 生成选题 / 进入 topic_picking** 时，**必须先**执行事实前置编排（降 Token、满足可审计来源）：

```bash
# 在 stream-gen 的 workspace 根下（本技能位于 skills/streamy-content-gen）：
cd skills/streamy-content-gen
python3 scripts/preflight_topic.py --direction '用户的自然语言方向'
```

- 脚本 **stdout** 为 **单一 JSON**：成功时 `ok: true` 且含 **`topic_payload`**（`source_context[]`、`candidates[]`、**`candidate_evidence_packs`**、`preflight_meta.snapshot_path` 等）与顶层 **`snapshot_path`**；失败时 `ok: false` 且含 **`error`**（`code` / `message` / `hint`），**不会输出整段 Python Traceback**。
- **快照缓存**：`preflight` 在 **6h 内**优先读 **`cache/snapshot/snapshot.json`**（与 `query_market_facts` 共用）；勿重复拉数。
- **证据包落盘（W5）**：用户选候选后 **优先** `--apply-topic-choice <N>` 或 helper **`apply-choice`**；落盘产物为 `drafts/.../candidate_evidence_pack.json`。`outline_refining` 前门禁检查该文件，缺失则 `EVIDENCE_PACK_REQUIRED_BEFORE_OUTLINE`。
- **legacy 回退**：仅当 `topic_candidates` 无 `candidate_evidence_packs` 时，才用 `preflight_topic.py --candidate-id <N> --topic-payload-file ...` + 分步 `set-chosen` / `set-evidence-pack-file`（或 `apply-choice --force-preflight`）。
- **后续步骤（禁止同轮续写大纲）**：证据包落盘、**稿件类型画像（`MEMORY_workflow` 步骤3A）**、user-style 门禁完成后，再推进 `outline_refining`。topic 回复仅给 **#DID + 三候选（标题/论点/论据）+ 选号**；证据包回复仅给所选方向包，不得混入其它候选或 D1/D2 详情。
- **安全批量 helper（可选，在 workspace 根）**：

```bash
python3 scripts/stream_gen_workflow_helper.py start-topic --direction '<方向>'
python3 scripts/stream_gen_workflow_helper.py apply-choice --draft <DID> --candidate-id <N>
python3 scripts/stream_gen_workflow_helper.py list-styles
python3 scripts/stream_gen_workflow_helper.py bind-style --draft <DID> --style-id <UUID>
python3 scripts/stream_gen_workflow_helper.py prevalidate-script --payload-file <script.json> [--draft <DID>]
python3 scripts/stream_gen_workflow_helper.py validate-script --draft <DID> --payload-file <script.json>
```

helper 只覆盖**非用户决策段**；候选选择、证据包确认、风格选择、定稿仍须在飞书停等用户。`start-topic` 默认不写 `/tmp` 长期副本（`--keep-run-artifacts` 可保留调试副本）。
- **弱耦合**：脚本通过 **subprocess** 调用相邻技能 `finance-source-ingest/scripts/ingest.py`（默认同级目录 `../finance-source-ingest`）；若部署布局不同，使用 `--finance-root` 指向该技能根目录。

## 飞书侧「去工程化」输出（P0 · 体验）

飞书里用户要的是**稿与决策点**，不是 Agent 的执行日志。

- **禁止**在飞书连续多条消息展开：读哪个 prompt 文件、`schema`/`validate-only` 的中间状态、「先校验 / 再次校验」、`draft_manager.py` 路径复述、英文试错句（如未知 CLI flag）。
- **允许**的可见粒度：每个业务里程碑 **尽量单条消息**（例如「方向证据包」整块、「大纲已落盘」整块、「逐字稿已落盘」整块）；若必须分两包，第二包只补「合规结果 + 下一步按钮式一句」。
- **稿件类型确认**：对用户展示 **中文选项**（大盘观点 / 投教 / 人设介绍）；用户选定后再在工具里用 `market_view` 等 slug 调用 `draft_manager update --set-content-type`。
- **工具报错**：用 **一句中文** 概括 `error_code` + 建议动作；不要把整段 stderr 或多次重试过程贴进飞书。
- **与 `script-feishu-display` 的关系**：逐字稿展示仍遵守该碎片（不用三反引号包全文等）；若用户**明确**只要口播、不要镜头行/附录，再按该碎片「按需」收敛展示（默认仍可按产品保留附录）。

## 稿件类型与结构模板（v0.2.3 · 可选）

选题与风格由用户/流程人工确认后，可按 **稿件类型** 使用「模块键 → 口播段」约束，降低逐字稿结构漂移。工具**不调用 LLM**，只生成 **JSON Schema + system/user 拼装 + 纯文本 Assembler + `segments[]` 草案**；模型在 Agent/网关侧按 `json_schema` 产出 JSON 后，再 stdin 交给 Assembler 或 `segments` 子命令。

```bash
# 在 workspace-stream-gen 根下（路径按部署调整）
python3 skills/streamy-content-gen/scripts/content_template_tool.py schema --content-type market_view
python3 skills/streamy-content-gen/scripts/content_template_tool.py prompt-bundle --content-type persona_intro --ip-id laoding
# 将 LLM 返回的模块 JSON 拼成口播或生成 segments 草案（duration 可按稿长调整）
echo '<modules_json>' | python3 skills/streamy-content-gen/scripts/content_template_tool.py assemble --content-type market_view
echo '<modules_json>' | python3 skills/streamy-content-gen/scripts/content_template_tool.py segments --content-type investor_edu --duration-sec 60
```

- **类型名**：`market_view` | `investor_edu` | `persona_intro`（配置见 `configs/content_templates/*.json`；新增类型=新增同 stem 的 JSON）。
- **IP**：`configs/ip_profiles/<ip_id>.json`；模板 `instruction` 中的 `{{ var }}` 须能在该 JSON 找到对应键，否则工具**报错退出**（不静默替换）。
- **与 `draft_manager` 衔接**：`segments` 输出仅为 `segments` + 元信息，写入 `script_refining` 前仍须补齐 `draft_manager schema --stage script_refining` 要求的顶层字段（`production_appendix` 等）并走 `--validate-only`。
- **meta 落盘（v0.2.3）**：`draft_manager.py create [--content-type market_view|investor_edu|persona_intro] [--ip-id <stem>]`；或 `update --draft <DID> --set-content-type <type> [--set-ip-id <stem>]`；清除 `--clear-content-profile`。与 `--set-style-id` / `--set-evidence-pack-file` 等原子 patch **每次只选一种**。`doctor --draft <DID>` 返回体含 `content_profile` 与 `profile_notes`（非阻断提示）。

## Draft ID 与目录契约（P0）

**正式稿件**的 `draft_id` **必须**为 `draft_manager` 分配并写入 `meta.json` 的 **三位 Base36**（如 `PBG`、`9WC`），目录形态为：

```text
drafts/active/default/<draft_id>/meta.json
drafts/active/default/<draft_id>/history.json
drafts/active/default/<draft_id>/topic_candidates.json   # topic_picking 起
```

全局登记见 **`drafts/index.json`** 的 `users.<id>.active_drafts`（其中只应出现 **上述三字符 id**）。

**禁止**把下列形式当作正式 `draft_id`（多为模型「手写落盘」绕过工具，**不会**与 `index.json` / 列表脚本对齐）：

- 在 **`drafts/` 根目录**新建 `20260424-某主题.json`、`*-outline.md` 等扁平文件；  
- 在 **`drafts/` 下**用 **日期 + 中文** 作文件夹名（如 `2026-04-24-降准预期银行股/`）并塞 `draft.json`；  
- 用 **用户可见长串** 代替三字符 id 参与「进行中的稿件」列表。

**正确做法**：新建稿一律走 **`draft_manager create`**（或你们环境中与之等价的唯一入口），后续 **`draft_manager update` / `set_chosen`** 只引用该命令返回/写入 `meta.json` 的 **`draft_id`**。主题、长标题只写入 `meta.json` / `topic_candidates` 等字段，**不要**把可读 slug 当目录名替代三字符 id。

## Draft 结构诊断与制作指导硬门禁（P0）

- 禁止直接写 `outline.md` / `script.md` / `meta.json` 推阶段；大纲、逐字稿必须通过 `draft_manager update --stage outline_refining|script_refining --payload-file <json>` 生成对应 `.json` 与 `.md`。
- 若怀疑历史稿绕过工具，先执行：

```bash
python3 skills/streamy-content-gen/scripts/draft_manager.py doctor --draft <DID> --json
python3 skills/streamy-content-gen/scripts/draft_manager.py doctor --draft <DID> --include-archive --since-days 7 --json
```

- `doctor` 报 `DIRECT_WRITE_OUTLINE_MD` / `DIRECT_WRITE_SCRIPT_MD` 时，该稿不能视为完成了 v0.1.8 制作指导；需要重新用结构化 payload 落盘，确保大纲 `production_hint` 与逐字稿 `production_appendix` 四块存在。
- 工具侧已拦截：进入 `script_refining` 前会检查上游大纲是否由 `draft_manager` 写过并包含 `production_hint`；`finalize` 前会检查逐字稿是否由 `draft_manager` 写过并包含 `production_appendix`。
- **降耗但不降质**：逐字稿生成前可先 `draft_manager prevalidate --stage script_refining --payload-file <json> --json`（或 helper `prevalidate-script`），再正式 `update` 前执行 `--validate-only --json`。逐字稿默认读 `prompts/fragments/script-core.md` + `prompts/fragments/script-min-schema.md`；附录每块 3–5 条、`evidence_source_type` 白名单见 `prevalidate` 返回的 `rules`。不得用 `role: "host"` 或跳过 `user_style_context` 绕过门禁。
- **会话费控（新增）**：定稿时优先使用  
  `python3 skills/streamy-content-gen/scripts/draft_manager.py finalize --draft <DID> --min-context-reset`  
  该开关会在归档后执行最小上下文清理（仅处理 `agents/stream-gen/sessions/*` 历史会话文件与失效会话索引，不触碰 `drafts/`、`memory/`、规则文件）。

## 与 finance-source-ingest 的关系

- `preflight_topic.py` 默认 `--out-dir /tmp/finance_data/`，并读取其中 **`snapshot.json`** 的 **`markdown_summary`** 折叠进 `source_context`（超长截断以降低 Token）。
- 完整「ingest → FactSnapshot → 手写 payload」高阶管道仍以 `adapter_ingest_to_fact_snapshot.py` 等为准；**带方向开稿** 优先走本脚本的 **轻量闭环**。
- `finance-source-ingest` 保持可迁移、脚本内不调用联网搜索。若 Agent 仍需人工核对缺口，由会话策略自行决定；不得把联网结果伪装为 ingest 原始信源或覆盖 `sections` 中的 API 数值。

## 个性化风格（user-style-manager，开稿硬门禁）

- **数据位置**：`{WORKSPACE_ROOT}/user_data/style_memory.db`（**不**在 `skills/` 内，便于 skills 分卷迁移时不带走用户库）。**禁止**把用户原文/切片写进本 `SKILL.md`。
- **user_id**：与 `draft_manager` 一致，优先环境变量 `OPENCLAW_USER_ID`；多用户/生产应配置，否则将共用默认用户下的风格行。
- **开稿前风格确认（P0）**：进入 `topic_picking` 后、推进 `outline_refining` 前，必须先列出当前所有可用 user-style 并询问用户选择。没有用户选择并绑定 `style_id` 时，不得直接生成大纲；不得用“默认风格/通用风格”绕过该步骤。
- **选风格**：用户**显式**说「用某风格」或从列表点选后，将对应 `style_id` 写入该 Draft 的 `meta`：
  - **建稿时**：`draft_manager create --style-id <UUID>`
  - **已存在 Draft**：`draft_manager update --draft <DID> --set-style-id <UUID>`；清空：同一命令加 `--clear-style`（与整阶段 `--stage` / `--set-chosen` 互斥，**分次**调用）
- **列可用风格（飞书选型 · 降耗）**：优先带预览，避免 `list` + `get-context` 两次 exec：

```bash
python3 skills/user-style-manager/scripts/style_cli.py list --with-context --json
# 或 helper：python3 scripts/stream_gen_workflow_helper.py list-styles
```

- **绑定风格 + 取上下文（一步）**：

```bash
python3 scripts/stream_gen_workflow_helper.py bind-style --draft <DID> --style-id <UUID>
```

返回 `user_style_context`，可注入大纲/逐字稿 prompt。分步等价：`update --set-style-id` 后 `get-context --format json`。

- **在进 outline / script 前注入风格**（P0）：`meta.style_id` 已设且提交 `outline_refining` / `script_refining` 时，payload 须含 **`user_style_context`**。若 payload 未带且已绑 `style_id`，`draft_manager update` 会**自动**调 `get-context` 注入。**不得**把敏感原文写入 `meta.json`（仅保留 `style_id` UUID）。

- **与 topic 阶段**：`topic_picking` **不**强制带 `user_style_context`；风格块主要在 **大纲 / 逐字稿** 阶段约束语气与例句。
- **工具硬拦**：`draft_manager update --stage outline_refining/script_refining` 在 `meta.style_id` 为空时会返回错误；正确补救是先列出风格并 `--set-style-id`，不要手写 `outline.md`、`script.md` 或直接改 `meta.json` 绕过工具。

## 迁移性

- 仅依赖：**本仓库 `scripts/preflight_topic.py`** + **兄弟目录 `finance-source-ingest`** + 系统 `python3`。
- **不侵入** ingest 的 CLI 契约（仅 subprocess 调用）；兄弟技能根目录可通过 `--finance-root` 覆盖。

细节与约束见兄弟技能 [`user-style-manager/SKILL.md`](../user-style-manager/SKILL.md)（本文件不重复其 CLI 全表）。
