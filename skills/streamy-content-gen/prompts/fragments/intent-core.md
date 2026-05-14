# Intent Core（默认读取）

> Agent 收到用户消息后的**第一步**：口语 → **意图名 + 命令**。与 `SKILL.md` 冲突时以 **SKILL.md** 为准。  
> **长表 / 全量话术样例**：`prompts/fragments/intent-examples.md`（按需）。

---

## 硬约束速览（勿跳）

- **逐级推进**：`topic_picking` → `outline_refining` → `script_refining`；跨阶段一次 `update` 会 `STAGE_SKIP_FORBIDDEN`。
- **选题数据源**：`topic_picking` 的 payload 须有真实 `source_context[]` / 各候选 `evidence_anchor`；黑名单词触发 `CANDIDATES_REQUIRE_DATA_SOURCES`。
- **Draft ID**：所有 mutating 命令的 `#<DID>` 必须来自当会话 **`list` / `create` 返回体**，禁凭记忆编造。
- **`create` 后首条回复必须含字面 `#<DID>`**。
- **`chosen` 唯一入口**：`update --set-chosen N`；**禁止** `edit` 改 `topic_candidates.json`。
- **列归档唯一入口**：`archive-list --json`；**禁止** `ls`/`find`/`list --include-archive` 脑补归档。
- **每次工具返回含 `result.invariants[]` 必读**。
- **飞书对用户输出去工程化**：禁止刷屏式汇报「读取规则 / 获取 schema / validate-only / 先校验 payload」等；禁止把英文 CLI 试错句或 stderr 原样贴给用户；里程碑尽量 **单条消息**（证据包 / 大纲 / 逐字稿）。稿件类型对用户用中文选项（大盘观点 / 投教 / 人设介绍），`market_view` 等 slug 仅用于工具调用。

---

## 0 · 识别策略

1. 关键词命中样例（不够则查 `intent-examples.md`）  
2. 语义泛化（「下一步」「继续」≈ 阶段推进）  
3. 复合句 → 按 §5 拆

**识别输出（内部）**：

```json
{
  "intent": "confirm_topic",
  "draft_id": "A3F",
  "params": {},
  "need_clarify": false,
  "side_intents": []
}
```

---

## 1 · 生命周期（任意阶段可触发，create 除外）

| 意图 | 典型口语 | 命令 / 动作 |
|---|---|---|
| `create_draft` | 开稿 / 出一条 / 想个内容 | `draft_manager.py create [--topic "..."] [--content-type market_view 等] [--ip-id <stem>]` |
| `list_drafts` | list / 几条在跑 | `list --json`；若问**历史/归档** → **`archive-list --since-days N --json`** |
| `switch_draft` | 切到 #B7K | `switch --draft <DID>` |
| `show_draft` | A3F 到哪了 | `show --draft <DID>` |
| `drop_draft` | 放弃 / drop | `drop --draft <DID> --reason "..."` |
| `finalize_draft` | 定稿 / OK 收 | 仅当 `stage=script_refining`：`finalize --draft <DID> --min-context-reset`；若在 `outline_refining` 说「定稿」→ 先澄清是否指「确认大纲→逐字稿」 |

### `create_draft` 补充

- 已有 active draft 时先问：继续还是新开。
- **易误判**：「60 秒怎么切入」「给一条口播」→ **开稿流水线**，禁在未选题/无证据包/无风格时输出**带分秒分镜的定稿口播**（见 SKILL 铁律 15）。
- **时效主题**：`topic` 含「今天/热点/涨跌」等 → `create` 后先拉数再跑 `topic-generation`：

```bash
python3 skills/streamy-content-gen/scripts/fetch_hot_rank.py --top 10 --json
python3 skills/streamy-content-gen/scripts/fetch_market.py --json
```

（路径以 workspace 根为准；若脚本不存在则按 SKILL 当前信源命令执行。）

- **降耗（可选）**：`python3 scripts/stream_gen_workflow_helper.py start-topic --direction "<原话>"` — 只到 `topic_picking` 落盘，**必须停**等用户选 1/2/3。

- **快讯/咨询后**：若用户未说「只要数字、别出稿」，回复末须有 **「下一步」**（开稿 / 要三选题 / 只看快讯）— 细节表见 `intent-examples.md`。

### `archive-list`（归档）

用户一提「以前/归档/历史那条」→ **先** `python3 skills/streamy-content-gen/scripts/draft_manager.py archive-list --since-days 30 --json`，只从返回 `drafts[].draft_id` 引用 ID；`count=0` 如实说。

---

## 2 · 阶段推进与回退

**先读 `meta.stage`。** 顺序记忆：

`preflight（方向）` → `topic_picking`（三候选）→ **用户选候选** → **方向证据包** → **稿件类型 + IP（meta 落盘）** → **user-style** → `outline_refining` → `script_refining` → `finalize`

### 2.0A 方向证据包（`candidate_evidence_pack_gate`）

前置：`topic_picking` 已落盘且用户选了序号。

1. `draft_manager.py update --draft <DID> --set-chosen <N>`
2. `preflight_topic.py --candidate-id <N> --topic-payload-file '...' --snapshot-path '...' --allow-targeted-fetch`
3. 展示 `evidence_pack`；并 `draft_manager.py update --draft <DID> --set-evidence-pack-file '<evidence_pack.json>'`
4. 缺口 `source_gaps[]` 如实提示，禁脑补。

可选：`scripts/stream_gen_workflow_helper.py apply-choice ...` 打包 1–2；**仍须**展示证据包并走 style 门禁。

### 2.0B 稿件类型与 IP 画像（v0.2.3 · 人工）

- **时机**：**证据包已落盘**、用户确认继续后，**在绑 `style_id` 之前**，询问用户本稿类型并写入 `meta`：  
  `draft_manager.py update --draft <DID> --set-content-type market_view|investor_edu|persona_intro [--set-ip-id <stem>]`；清除：`--clear-content-profile`。`persona_intro` 或与模板中 `{{ var }}` 同时出现时**应**带 `--set-ip-id`（对应 `configs/ip_profiles/<stem>.json`）。
- **分模块口播（可选链路）**：`python3 skills/streamy-content-gen/scripts/content_template_tool.py prompt-bundle --content-type ... [--ip-id ...]` 得到 `system` / `user` / `json_schema`；**由 OpenClaw 会话内模型**按 schema 产出模块 JSON；`assemble` / `segments` 仅做机械拼装，**不在 skill 脚本内直连 LLM**。

### 2.1 `confirm_topic`（`stage=topic_picking`）

- 展示候选须含 **title + thesis + 三条 evidence**（禁只贴标题）。
- 默认回复**不**拼大盘/快讯块（除非用户要看数据来源）。
- **默认路径**：`set-chosen` → §2.0A 证据包 → **§2.0B 稿件类型 + IP（`update --set-content-type`）** → `style_cli list` → 用户选 → `update --set-style-id` → 读 `outline-generation.md` 索引 → 生成大纲 → `update --stage outline_refining ...`
- **禁**：未展示证据包或未绑 `style_id` 就写大纲；禁手写 `outline.md` / 跳 `draft_manager`。

### 2.2 `regenerate_topics`

`stage=topic_picking`：重跑 `topic-generation.md`，`update --stage topic_picking` 覆盖（带用户新约束 + 旧候选作 context）。

### 2.3 `confirm_outline`（`stage=outline_refining` → 逐字稿）

读 `script-generation.md` 索引 + `script-core` + `script-min-schema`；组最小 payload；**先**：

```bash
python3 skills/streamy-content-gen/scripts/draft_manager.py update \
  --draft <DID> \
  --stage script_refining \
  --payload-file /tmp/script-<DID>.json \
  --validate-only \
  --json
```

通过后去掉 `--validate-only` 正式 `update`。`errors[]` 须一次修完。合规读 `result.compliance`；**禁**往 script payload 塞 `display_markdown`；**禁**全写 `role:"host"` 绕过证据字段；有 `user_style_context` 须补 `production_style_adaptation`。

回复边界：`script_refining` 默认只展示逐字稿+制作附录+合规+下一步；**不**重复选题阶段市场块。

### 2.4 `rewind_to_topic`

「换方向/重选/再想一个」**默认**当前稿回退（`rewind_to_topic`），**不是** `create`，除非用户明说「新开一条/另起一条」或 focus 已在 `topic_picking`（则倾向 `regenerate_topics`）。

```bash
python3 skills/streamy-content-gen/scripts/draft_manager.py update --draft <focus> --stage topic_picking \
  --payload-file <新 topic_candidates.json> \
  --edit-note "回退到选题：<原因>"
```

Agent 话术避免「新开一条」歧义；用「回退到选题阶段」。

### 2.5 `regenerate_stage`

整段重写当前阶段：重跑 outline 或 script 对应 prompt 后 `update` 覆盖（与 §3 局部 `edit_content` 不同）。

---

## 3 · `edit_content`（局部改）

不调额外汇总脚本：理解语义 → 改 JSON → `update` + 清晰 `--edit-note`。改逐字稿用 `--stage script_refining`（扫描内嵌）。定位：Hook→`hook`；论据 N→`argument_N`；转折→`turn`；CTA→`cta`；时间点对齐 `segments[].time`。

---

## 4 · 元操作

| 子意图 | 动作 |
|---|---|
| `query_status` | 读 `index.json` focus → `show` → 人话汇报 |
| `query_history` | 读 `history.json` 摘要 |
| `help` | 极简能力说明，禁贴全文 SKILL |

### 4.4 `query_market_facts` — 非开稿、全量 ingest（**硬契约**）

| 用户表述 | 举例 |
|---|---|
| 今日盘面 / 热点 / 信源快照 / 道指等 | 「今天大盘」「拉今日行情」「热榜是啥」 |

**动作**：**不**激活三段式，**不** `create`。在 workspace 下执行：

```bash
python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --max-items 30 --summary-only
```

- **禁止**只跑 `fetch_market` / `fetch_hot_rank` 拼半套；**禁止**按用户只拉 `market` 或只拉 `news`。即使用户只说「行情」或「热点」，仍 **`market,news,social` 全量同一命令**。
- **必须**将返回 JSON 的 **`markdown_summary` 全文原样**发给用户（可加一行数据来源/时间）；不得删板块、不得拆两条消息各贴一半；不得因有占位行整段省略板块。
- 结尾可问「要不要就这个开一条稿？」— 用户同意再走 `create_draft`。

---

## 5 · 复合意图

**优先级**：`生命周期动作 > 阶段推进 > 内容修改 > 元操作`

| 用户句子 | 顺序 |
|---|---|
| 「定了②，Hook 改冲」 | 先 `confirm_topic`（含证据包/style 门禁）再 `edit_content` |
| 「A3F 丢了，开个聊 AI 的」 | `drop` → `create` |
| 「出逐字稿，60 秒」 | `confirm_outline` + `time_budget_sec` |
| 「list 然后切最新」 | `list` → `switch` |

有依赖先执行依赖；不自作主张丢次级意图。

---

## 6 · 焦点消歧

| 情况 | 处理 |
|---|---|
| 显式 `#XXX` | 用该 ID |
| 无 ID 且有 `focus` | 用 focus |
| 无 focus、仅 1 条 active | 用该条 |
| 无 ID、≥2 条 active | **反问**一条 |
| 无 active 且意图是改稿 | 反问；`create` 可直接走 |

**一次对话最多反问一次**；模板话术见 `intent-examples.md` §6.2。

---

## 7 · 「看起来像但不要走本 skill」**

| 输入 | 正确响应 |
|---|---|
| 写通用代码 / 选股结果 / 对标账号 / 小红书图文独立体裁 | 非本 skill 或未实装，简短说明 |
| 今天大盘 / 热榜 | **§4.4** 全量 `query_market_facts`，原样 `markdown_summary` |
| 「你能干嘛」 | `help` 级短答 |

---

## 8 · 识别失败兜底

1. 不要硬套意图  
2. 一句澄清 A/B  
3. 仍不清 → 建议 `list` 或说明能力边界  

---

## 9 · 与 prompt 索引的衔接

- 大纲：`prompts/outline-generation.md`  
- 逐字稿：`prompts/script-generation.md`  
- 选题：`prompts/topic-generation.md`
