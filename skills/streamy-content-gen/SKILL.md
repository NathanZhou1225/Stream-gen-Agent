---
name: streamy-content-gen
description: |
  Streamy 短视频/口播：Draft 多阶段（topic_picking → outline_refining → script_refining）与合规闸；支持 finance-source-ingest 事实管道。
  带明确方向开稿：须先 preflight_topic.py；topic_picking 当轮仅展示候选（每条必须含标题+核心论点+3条论据）；draft_id 须为 draft_manager 三字符 + active/default 目录；禁止同轮越级输出大纲/逐字稿/分镜成稿。
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
   执行完 `preflight_topic.py` 的 **同一轮** 助手工作中：允许 **shell 跑脚本** + **`draft_manager update --stage topic_picking` 一次** + 对用户的 **短回复**。  
  短回复仅保留：  
   - **① 可选选题方向**（`candidates`：每条含标题 + 核心论点 + 3 条论据）；  
   - **②** 一句「请回复序号选定选题后再进大纲」。  
  默认**不展示**信源状态/大盘行情/市场焦点/事实摘要（除非用户显式要求回看数据来源）。  
   **唯一例外（纯拉数 / 非 topic_picking）**：用户仅要「今日行情 / 热点 / 全量 / 信源快照」**任一口径**且**未**进入带方向开稿链时，按 **`prompts/natural-language-intent.md` §4.4**：必须执行 **`python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --max-items 30`**，并将返回 JSON 的 **`markdown_summary` 全文原样** 展示（不得拆成只行情或只热点、不得拆两条消息、不得自行重排版删段）。  
   **联网补充（确定性脚本）**：`query_market_facts.py` 会先调用 `finance-source-ingest`，再依据 `meta.websearch_required/gaps` 直接调用 **`tavily-search`** 的 `scripts/search.mjs`，并把 **「联网补充（Tavily 兜底）」** 拼入 `markdown_summary`。纯拉数场景禁止直接调用 `finance-source-ingest/scripts/ingest.py` 后把 Tavily 留给模型自行决定。  
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

- 脚本 **stdout** 为 **单一 JSON**：成功时 `ok: true` 且含 **`topic_payload`**（内含 `source_context[]` 与 `candidates[]`：`evidence_anchor` 绑定 **不同** 列表行，`title` 为 **可区分的规则钩子**（快讯/指数/讲述视角组合，**非**「用户原句 + ·解读/·影响」）；失败时 `ok: false` 且含 **`error`**（`code` / `message` / `hint`），**不会输出整段 Python Traceback**。
- **后续步骤（仅此一步，禁止同轮续写大纲）**：将返回体中的 **`topic_payload`** 作为 **`draft_manager update --stage topic_picking`** 的 JSON 体（字段对齐：`source_context`、`candidates`），**写入 `drafts/`**。对用户侧回复仅给 **draft 状态 + 候选标题 + 每条核心论点 + 每条 3 条论据 + 选号指令**（详见上文「单次回复边界」），**不得**在同一轮继续生成大纲或逐字稿。
- **弱耦合**：脚本通过 **subprocess** 调用相邻技能 `finance-source-ingest/scripts/ingest.py`（默认同级目录 `../finance-source-ingest`）；若部署布局不同，使用 `--finance-root` 指向该技能根目录。

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

## 与 finance-source-ingest 的关系

- `preflight_topic.py` 默认 `--out-dir /tmp/finance_data/`，并读取其中 **`snapshot.json`** 的 **`markdown_summary`** 折叠进 `source_context`（超长截断以降低 Token）。
- 完整「ingest → FactSnapshot → 手写 payload」高阶管道仍以 `adapter_ingest_to_fact_snapshot.py` 等为准；**带方向开稿** 优先走本脚本的 **轻量闭环**。
- `finance-source-ingest` 保持可迁移、脚本内不调用 Agent WebSearch；若迁移到具备 WebSearch 的 Agent，需同时迁移 `AGENTS.md` / `MEMORY.md` / `natural-language-intent.md` 中的兜底协议。WebSearch 补充优先依据 `meta.websearch_required` / `meta.websearch_gaps` 触发，只可作为 **标注来源的辅助事实**，不得伪装为 ingest 原始信源。

## 个性化风格（user-style-manager，可选）

- **数据位置**：`{WORKSPACE_ROOT}/user_data/style_memory.db`（**不**在 `skills/` 内，便于 skills 分卷迁移时不带走用户库）。**禁止**把用户原文/切片写进本 `SKILL.md`。
- **user_id**：与 `draft_manager` 一致，优先环境变量 `OPENCLAW_USER_ID`；多用户/生产应配置，否则将共用默认用户下的风格行。
- **选风格**：用户**显式**说「用某风格」或从列表点选后，将对应 `style_id` 写入该 Draft 的 `meta`：
  - **建稿时**：`draft_manager create --style-id <UUID>`
  - **已存在 Draft**：`draft_manager update --draft <DID> --set-style-id <UUID>`；清空：同一命令加 `--clear-style`（与整阶段 `--stage` / `--set-chosen` 互斥，**分次**调用）
- **列可用风格**（在 workspace 根，路径按部署调整）：

```bash
python3 skills/user-style-manager/scripts/style_cli.py list
```

- **在进 outline / script 前注入风格**（P0 契约）：当 `meta.style_id` 已设置、且本轮要向 `outline_refining` 或 `script_refining` 提交产物时，**先**取 RAG 文本，再组 payload 顶层字段 **`user_style_context`**（**字符串**，整段可粘贴进系统/用户补块）：

```bash
python3 skills/user-style-manager/scripts/style_cli.py get-context --style-id <UUID>
# 可选：python3 ... get-context --style-id <UUID> --format json
```

将 **stdout** 的文本块原样或略排版写入当日 `update --payload-file` 的 JSON 的 `user_style_context`。**不得**把敏感信息写入 `meta.json`（`meta` 只保留 `style_id` UUID）。

- **与 topic 阶段**：`topic_picking` **不**强制带 `user_style_context`；风格块主要在 **大纲 / 逐字稿** 阶段约束语气与例句。

## 迁移性

- 仅依赖：**本仓库 `scripts/preflight_topic.py`** + **兄弟目录 `finance-source-ingest`** + 系统 `python3`。
- **不侵入** ingest 的 CLI 契约（仅 subprocess 调用）；兄弟技能根目录可通过 `--finance-root` 覆盖。

细节与约束见兄弟技能 [`user-style-manager/SKILL.md`](../user-style-manager/SKILL.md)（本文件不重复其 CLI 全表）。
