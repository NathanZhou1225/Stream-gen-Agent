# 自然语言意图识别清单（v0.1.5）

> Agent 收到用户消息后的**第一步**。本文件把常见口语映射到**具体命令 / prompt 调用**，让 Agent 少临场思考、多查表执行。
>
> 与 `SKILL.md` § 2 的三段式流水线一一对齐；一切不匹配以 SKILL.md 为准。
>
> **v0.1.5 变更速览**：
> - **§1.1 `create_draft` / 开新稿**：问「X 秒口播**怎么切入** / 给一条**口播** / **怎么讲** 今日」时，**默认 = 要开稿走流水线**，不是「当场给录制定稿」。见 §1.1 的「易误判表」+ SKILL §1.3 / 铁律 15 —— **禁止**在选题/大纲未落盘时，纯聊天输出**带分秒/分镜全文的口播**
>
> **v0.1.4 变更速览（勿跳读）**：
> - §2.1 `confirm_topic` / §2.3 `confirm_outline`：**跨阶段 forward 跳阶段**（如 topic_picking 一次性 update 到 script_refining）会被工具返 `STAGE_SKIP_FORBIDDEN`。必须逐级推进
> - §2.1 `topic_picking` 的 `update --payload-file` 里**必须**有真实数据源（`source_context[]` 或每个 candidate 的 `evidence_anchor`），命中 brainstorm/llm/memory/脑补/拍脑袋 等黑名单关键词会被工具返 `CANDIDATES_REQUIRE_DATA_SOURCES`
> - §2.1/§2.2/§3.x 所有 mutating 命令（update/switch/finalize/drop/set-chosen）前置 DRAFT_NOT_FOUND_IN_SESSION 校验：**Draft ID 必须来自 `list` / `create` 的返回体**，不得凭上下文记忆或旧会话"想当然"
> - 所有 tool result 里有 `result.invariants[]` 字段，**每次调完必读**（比脑内记 SKILL.md 便宜）
>
> **v0.1.3 变更速览**（保留做 context）：
> - §1.2 **列归档改用 `archive-list` 独立命令**（禁脑补、禁 ls/find，上升到最严禁令）
> - §2.1 `create_draft` 成功后 Agent 第一行回复**必须**显式写 `#<DID>`
> - §2.1 `confirm_topic` 如果只想"标记选中候选不推进"走 `update --set-chosen N`（**禁止**用 `edit` 改 `topic_candidates.json`）
> - §2.3 `confirm_outline`：`update --stage script_refining` **已内置**合规扫描 + 回写，Agent 无需再单独调 `lite_compliance_scan.py`
> - §3.1 `edit_content`：同上，修改逐字稿后**也无需**手动调扫描

---

## 0 · 识别策略总纲

**三层识别**（从硬到软）：

1. **关键词命中**：先扫本文件的"用户表述样例"列，命中则直取对应意图
2. **语义泛化**：样例只是代表，用户同义表达（"下一步/继续/往下"都等价"推进"）靠 Agent 本体语义理解
3. **组合拆解**：一句话包含多个意图（如"定了②，再把 Hook 改冲一点"）→ 按下面 § 5 拆

**识别输出**：

```json
{
  "intent": "confirm_topic",          // 本文件列出的意图名
  "draft_id": "A3F" | null,           // 显式或从 focus 推断；null 触发反问
  "params": { ... },                  // 意图特有参数（如 selected_index=2）
  "need_clarify": false,              // true 时走 § 4 反问
  "side_intents": []                  // 复合时的次要意图，见 § 5
}
```

---

## 1 · 创建 / 生命周期意图

Draft 生命周期相关，**任意阶段**都可触发（除了 create 本身不需要已有焦点）。

### 1.1 `create_draft` — 开新 Draft

| 用户表述 | 举例 |
|---|---|
| 主动开新 | "出一条抖音稿" · "给我出个口播" · "开个新 draft" · "来一条短视频" · "今天要录一条" |
| 带主题开新 | "帮我出一条聊降准的" · "写个关于 AI 算力的稿子" · "出一条美联储议息的短视频" |
| 模糊表达 | "今天录啥好" · "帮我想个内容" |

**Agent 动作**：
```bash
draft_manager.py create [--topic "<主题>"]
```

**提取参数**：
- `topic`（可选）：若用户明说主题（"关于降准""聊美联储议息"），提取成字符串；否则空
- 若同时检测到"抖音/视频号/小红书/公众号/B站"等平台词：**忽略**（v1 不做平台差异化），只在回复里口语接话不写入 meta

**前置校验**：无前置阶段要求。若 `draft_manager list` 显示已有 active draft，Agent **主动提示**："当前还有 #A3F 进行中，开新的还是继续 A3F？"

**⚠️ 硬规则（v0.1.3 Issue #2 修复）· Draft ID 必须显式展示**：

`draft_manager create` 返回 `result.draft_id` 后，Agent 给用户的**第一句/第一行**回复**必须**包含 `#<DID>` 字面串（例如"已开 **#X2E**，我们来聊..."）。

**严禁**：
- ❌ 创建了 Draft 却整条回复都不提 ID（用户后续如果想 switch/show 会陷入"我哪条"困境）
- ❌ 用"新稿子"/"这条"等代词替代 ID

**推荐话术模板**（任选其一）：
- "已开 **#X2E**（选题阶段），接下来...
- "新稿 **#X2E** 已创建，我们从以下三个方向里挑一个..."

**时效性主题加强规则（v0.1.3 新增 P1-E，关联 SKILL.md 铁律 11）**：

若 `topic` 含**时效词**（"最近 / 今天 / 本周 / 热点 / 涨跌 / 热榜 / 现在"等），create 之后**必须**先调数据脚本再跑 topic-generation：

```bash
python3 scripts/fetch_hot_rank.py --top 10 --json
python3 scripts/fetch_market.py --json           # 若涉及 A 股/海外/板块
```

**严禁**：凭 LLM 训练知识"脑补"最近的热点/板块/涨跌；训练截止日与今天必然有 gap，给用户错数据会严重失信。

**⚠️ 流程铁律（v0.1.5+ · 与 SKILL.md §1.3、铁律 15 一致）· 禁止「咨询句」变「全稿」**：

| 用户说法（易误判） | 正确定位 | **禁止** |
|---|---|---|
| 「60 秒口播**怎么切入**」/「**怎么讲** 今天 A 股」/「给一条 **X 秒** 口播」 | 等同 **开新稿 + 还没选题**，走 `create` → 拉数（若含时效/行情）→ `topic_picking` **三候选落盘** → 等用户选序号 | 在**未** `update --stage topic_picking` 前，用纯对话输出**带分秒/分镜的完整口播**（如 `[0-5s]…[56-60s]…`），即跳过选题与大纲 |
| 用户只要「灵感/要点，**不要**落盘 / 不要开 Draft」 | 可只给**要点或结构提示**，不调用 `create`；**仍不要**给可直录的「定稿式」全分镜，除非用户明确要「按镜头发一版范文且不落盘」 | 无 |

若识别到自己在「无 Draft 或 stage 仍在 topic/outline」时已经写了全篇口播，**下一回合**须：道歉说明应用三段式 → `list` → 必要时 `create` → 从 **§2.1 选题** 续跑。

**✅ 快讯/咨询后必接一步（v0.1.7+ · 与 SKILL §1.4、SOUL 领航员、AGENTS L0 一致）**：

| 用户说法 | 你列完事实后**还必须** |
|---|---|
| 「发我今日热点」「今日快讯讯息」「有什么要闻/可讲的」等（**未**说只要数字、别出稿） | 回复**末尾**加 **「下一步」** 段：用户可回 **「开稿」/「要三个选题」** → 走 `create` + `topic_picking`；或回 **「只看快讯」** 表示今天不产出。 **禁止** 只贴行情+财联社表就结束。 |

---

### 1.2 `list_drafts` — 列出当前所有稿

| 用户表述 | 举例 |
|---|---|
| 直接列 | "list" · "我手上有哪些稿" · "当前草稿" · "几条在跑" · "列一下" |
| 带疑问语气 | "我这有几个 draft 来着" · "是不是还有别的稿没弄完" |
| 带归档询问 | "包括之前定稿的" · "历史上所有的" · "看看归档" · "之前做过哪些" |

**Agent 动作**：

```bash
# 默认（只列 active）
draft_manager.py list --json

# 用户问到历史 / 归档 / "之前做过" / "以前那条" / 核对错误 ID — v0.1.3 起走独立命令
draft_manager.py archive-list --since-days 30 --json
```

**回显要点**：按 `focus` 标星，展示 `{id, stage, topic, last_updated}`。

---

**⚠️ 最强硬规则（v0.1.3 P0-D 修复，v0.1.2 失效后升级）· 列归档稿的唯一合法入口**

用户只要问到"**以前 / 之前 / 历史 / 归档 / 上次 / 之前那条 / 过往 / 以前做过的**"等历史相关表达，或需要核对某个 Agent 声称存在的归档稿件是否真实存在时，Agent **必须**：

1. **先调 `draft_manager.py archive-list --json`**（可加 `--since-days N` 调窗口）
2. 从返回的 `result.drafts[]` **结构化读** `draft_id + topic + stage + archive_date + drop_reason`
3. `count=0` 时如实告诉用户"近 N 天没有归档稿"，**不要**硬凑答案

**绝对禁止**（违反任一即为重大失信）：
- ❌ **用 `ls` / `find` / shell 任何命令手动探查 `drafts/archive/` 目录**（即便是想"确认一下"也不行）
- ❌ **凭对话历史、上下文记忆、用户以前提到过的 draft_id 脑补"这条应该在归档里"**。对话记忆和磁盘状态**随时会漂移**（用户可能手动清过盘、切换过端口、换过账号），对话记忆不可作为归档真值来源
- ❌ 在 Agent 的**回复文本**里编造不存在的 `#XXX` 形式的归档 ID。本命令返回的 `drafts[].draft_id` 是**唯一**能出现在回复里的合法归档 ID 来源
- ❌ 用 `list --include-archive`（v0.1.2 留存的兼容语法，v0.1.3 起**在归档场景禁用**；`list` 专用于 active；归档**必须**走独立命令，避免语义混淆）

**话术模板**（count=0 时）：
> "我查了近 30 天归档（`archive-list`），没有找到你说的那条。要不要我帮你在 active 里列一下？"

**话术模板**（count>0 时）：
> "近 30 天归档 N 条：#HFP（股市复盘-A，finalized，04-22）、#UCP（小红书-B，dropped，04-22）…"

---

### 1.3 `switch_draft` — 切换焦点

| 用户表述 | 举例 |
|---|---|
| 显式切换 | "切到 B7K" · "继续 #B7K" · "focus B7K" · "换到那条 Hello 的稿" |
| 隐式回归 | "接着刚才那条" · "刚才哪条来着" |

**Agent 动作**：
```bash
draft_manager.py switch --draft B7K
```

**消歧规则**：
- 用户说 "那条 XXX 的稿" → 先 `list` 匹配 topic_preview，找到再 switch；找不到反问
- 用户不给 ID 也不给主题 → 反问"你想切到哪条？"附 list 结果

---

### 1.4 `show_draft` — 查看某稿进度

| 用户表述 | 举例 |
|---|---|
| 查进度 | "A3F 到哪了" · "show A3F" · "A3F 现在进度" · "那个降准稿到哪一步了" |
| 回显产物 | "再给我看一遍 A3F 的大纲" · "A3F 的逐字稿发我" |

**Agent 动作**：`draft_manager.py show --draft A3F`

**差异处理**：
- 只问进度 → 简要回 stage + 最新 edit_note
- 要求看产物 → show 之后读对应 `outline.md` / `script.md` 渲染给用户

---

### 1.5 `drop_draft` — 放弃

| 用户表述 | 举例 |
|---|---|
| 显式放弃 | "放弃 #A3F" · "drop A3F" · "删掉这条" · "这条不要了" |
| 带原因 | "A3F 选题不行，丢了" · "这个 Hook 太烂，重来（隐式 drop + 开新）" |

**Agent 动作**：
```bash
draft_manager.py drop --draft A3F --reason "<用户说的原因或空>"
```

**注意**：用户说"重来/另起"时识别为**复合意图**（drop + create，见 § 5）。

---
### 1.6 `finalize_draft` — 定稿

| 用户表述 | 举例 |
|---|---|
| 显式定稿 | "定稿" · "就这样" · "OK 收" · "final" · "确认归档" |
| 阶段尾确认 | "可以了" · "就这版吧" · "没问题" |

**前置校验**：当前阶段必须 = `script_refining`（SKILL.md 状态迁移表约束）。若在 `outline_refining` 阶段用户说"定稿"，反问："大纲阶段定稿是指确认大纲推进到逐字稿吗？"→ 走 `confirm_outline`。

**Agent 动作**：`draft_manager.py finalize --draft <focus>`

---

## 2 · 阶段推进 / 回退意图

**受当前 stage 严格约束**。Agent 必须先读 `meta.stage` 再决定如何响应。

### 2.1 `confirm_topic` — 选定选题方向

**前置**：`stage = topic_picking`

**topic_picking 回复边界（新增）**：
- 在用户尚未选定候选时，回复只保留：`#<DID>` + 三个候选（标题/核心论点/3条论据）+ 选号引导。
- 默认不附加「信源状态 / 大盘行情 / 市场焦点 / 事实依据」块；仅当用户明确要求“再看数据来源/快讯”时再补。

**⚠️ 硬规则（新增，飞书展示强约束）**：

在 `topic_picking` 阶段给用户展示候选时，**不得只展示“标题/切入角度”**。每个候选必须至少包含：

1. `title`（标题）
2. `thesis`（核心论点，一句话）
3. `evidence` 三条（论据 1/2/3，可精简展示）

若 payload 已包含 `thesis/evidence` 但回复中省略，视为流程违规；必须重发本轮候选展示。

| 用户表述 | 举例 | 解析 |
|---|---|---|
| 序号选 | "就 ②" · "选第二个" · "2" · "选 B" | `selected_index = 2` |
| 描述选 | "那个反直觉的不错" · "数据派那个" | 匹配候选的 `hook_type` / `display.title` |
| 带微调 | "② 不错，再加一点历史对比" | 意图=`confirm_topic` + `selected_index=2` + `tweak="加历史对比"`（顺带触发阶段 B 跑 outline-generation 时把 tweak 写进 context） |

**两种子场景**：

**A. 直接推进到大纲**（默认，95% 的场景）：
1. 确定 `chosen_topic`（候选 JSON 里取）
2. 跑 `prompts/outline-generation.md`，输入 `{chosen_topic, user_tweaks, ...}` 生成大纲 JSON
3. `draft_manager.py update --stage outline_refining --payload-file ... --edit-note "选择候选 ②..."`
   （`update` 转阶段后 `topic_candidates.json.chosen` 的具体值对后续已无影响，不需要单独改）

**回复边界（新增硬规则）**：
- 进入 `outline_refining` 后，默认只展示「大纲 + 制作提示 + 确认指令」。
- **不要**在同一条大纲回复里继续附加「信源状态 / 大盘行情 / 市场焦点 / 事实依据」版块。
- 仅当用户明确要求"再看数据/来源/快讯"时，才补充事实块（建议单独一段，避免喧宾夺主）。

**B. 只想记录"用户选中了 N 号候选"但不推进**（罕见，比如用户想先确认选择、延后跑 outline）：

```bash
draft_manager.py update --draft <DID> --set-chosen <N> --edit-note "用户口头确认选 ②"
```

**⚠️ 硬规则（v0.1.3 P0-C 修复）· `topic_candidates.json.chosen` 写入白名单**：

修改 `topic_candidates.json.chosen` 字段**唯一合法入口**是 `update --set-chosen N`。

**严禁**：
- ❌ 用 `edit` / `str_replace` 等文件工具直接改 `topic_candidates.json`（v0.1.2 观察到 Agent 曾用 `edit` 强改 chosen=2，v0.1.3 堵死）
- ❌ 用 `update --stage topic_picking --payload-file` 只为改 chosen（这条会**覆盖整个 JSON**，容易误伤 candidates 数组）

`--set-chosen` 是**单字段原子 patch**：只改 chosen、自动记 history（action=set_chosen，含 prev/new）、顺带刷 meta.topic 为所选候选 title。阶段限制：**仅在 `topic_picking` 阶段可用**，进入 outline 后需先 rewind。

---

### 2.2 `regenerate_topics` — 再来一批选题

**前置**：`stage = topic_picking`

| 用户表述 | 举例 |
|---|---|
| 全换 | "换一批" · "再来三个方向" · "都不行，重新给" · "换个思路" |
| 带约束 | "再来三个，都要更情绪化" · "换三个带具体数字的" |

**Agent 动作**：重跑 `prompts/topic-generation.md`，context 带上**旧候选 JSON** + **用户约束**，避免重复。`update --stage topic_picking` 覆盖写入。

---

### 2.3 `confirm_outline` — 大纲定稿 → 逐字稿
**前置**：`stage = outline_refining`

| 用户表述 | 举例 |
|---|---|
| 推进 | "出逐字稿" · "下一步" · "继续" · "往下走" · "OK 出稿" |
| 带时长提示 | "出一版 60 秒的" · "做成 90 秒" | 额外参数 `time_budget_sec=60/90` |
| 带 CTA 提示 | "CTA 走加微那条" · "结尾引导关注就好" | 额外参数 `cta_preference` |

**Agent 动作**（v0.1.3 简化，合规扫描已内嵌）：

```bash
# 1. 跑 prompts/script-generation.md 生成结构化 segments[] JSON（不含 display_markdown）
# 2. 一条命令完成：落盘 script.json + 自动渲染 script.md + 自动合规扫描 + 回写 compliance
draft_manager.py update --draft <DID> --stage script_refining \
    --payload-file /tmp/script-<DID>.json \
    --edit-note "初版逐字稿"
```

返回结果的 `result.compliance = {status, warnings_count, warnings}` 即是合规扫描结果，Agent **不需要**再单独调 `lite_compliance_scan.py`（v0.1.3 P0-A：内嵌 + 自动 write-back）。

**回复边界（新增硬规则）**：
- `script_refining` 回复默认只展示「逐字稿 + 制作附录 + 合规状态 + 下一步确认」。
- **不要**重复 `topic_picking` 的市场讯息块（信源状态/大盘/快讯/候选依据）。

**渲染给用户的合规状态**直接读 `result.compliance.status`：
- `pass` → 回复结尾加 "合规扫描：🟢 通过"
- `warn` → 回复结尾加 "合规扫描：🟡 命中 N 处：…（逐条列 matched + at_time），要不要改？"

**严禁**（v0.1.3 硬约束）：
- ❌ 把 `display_markdown` 字段塞进 `update` 的 payload。v0.1.3 起 `script.md` **完全**由工具从 `segments[]` 按固定模板渲染，Agent 传了这个字段会被**静默丢弃 + stderr 告警 + 返回 deprecation_warnings**，白白浪费 token。如需未来做个性化脚本模板，走 `template_id + structured vars`，不回退到 Agent 写 markdown。
- ❌ 用 `edit` 工具改 `script.json.compliance` / `script.md`。

---
### 2.4 `rewind_to_topic` — 回退到选题

**前置**：`stage = outline_refining` 或 `script_refining`（任何非 topic_picking 阶段，只要 focus 还 active）

| 用户表述 | 举例 |
|---|---|
| 显式回退 | "换一个方向" · "重来选题" · "这个方向不行" · "换个主题" · "重新选吧" · "再想一个" |
| 隐式 | "大纲跑偏了，从头来" · "这个思路不对" |

**⚠️ 消歧规则（v0.1.2 新增，Issue #5）**：

用户说"**换方向 / 重选 / 改主题 / 再想一个**"这类表述时，**默认解读为 `rewind_to_topic`（在当前 focus draft 上回退），不要解读为 `create_draft`（新开一条）**，除非：

1. 用户明确说了"**新开一条 / 另起一条 / 新建 / 再开一条**" → `create_draft`
2. focus 为空或 focus 的 stage 已经 = `topic_picking` → `regenerate_topics`（在当前 draft 上重生成候选）
3. 用户说"把 #XXX 留着" → 保留 + `create_draft`

**话术避雷**：Agent 自己在回复里也**不要**用"重新开一条稿/新开一条"这类话，除非真的要 create。回复用"**我把 #XXX 回退到选题阶段，重新给你出方向**"这样明确表达回退语义。

**Agent 动作**：
```bash
draft_manager.py update --draft <focus> --stage topic_picking \
    --payload-file <重生成的 topic_candidates.json> \
    --edit-note "回退到选题阶段：<原因>"
```

（`update` 会自动清理 `outline.*` + `script.*` 四个 forward artifacts，并在 `history.json` 记 `cleaned_forward` 数组。无需 Agent 手动删文件。）

---
### 2.5 `regenerate_stage` — 整段重写当前阶段

**前置**：任意已生成阶段

| 用户表述 | 举例 |
|---|---|
| 大纲重写 | "大纲整个重来" · "outline 全部重写" |
| 逐字稿重写 | "稿子重写一版" · "换个风格再来一遍" |

**Agent 动作**：重跑对应 prompt（带用户约束 + 旧版为 context 参考），`update` 覆盖写。和 `edit_content`（§ 3）的区别：整段重写 vs 局部修改。

---

## 3 · 内容修改意图（自然语言增删改）

### 3.1 `edit_content` — 局部修改

**前置**：当前阶段已有产物

**Agent 动作**：**不调额外脚本**，由 Agent 本体语义理解 → 在内存里改 → `update` 落盘 + `edit-note` 写清改了什么。

#### 修改点分类（供 Agent 理解）

| 子类型 | 用户表述 | 处理 |
|---|---|---|
| `remove_section` | "论据 2 去掉" · "转折那段删了" | 从 segments / outline 删对应项，其它顺延 |
| `add_section` | "加一段聊历史对比" · "补个数据支撑" | 生成新段落插入，位置靠语义（"加一段在论据 1 后面"有位置提示） |
| `rewrite_section` | "Hook 再冲一点" · "CTA 换成私信引导" · "开头改短一点" | 定位段落 → 重写 |
| `tone_adjust` | "整体再口语化一点" · "严肃一点" · "加点情绪" | 全文微调 |
| `factual_fix` | "数据是 +2.1% 不是 2.3%" · "时间错了是 2025 年" | 定位事实点 → 修正 |
| `length_adjust` | "再精简 20 秒" · "拉长到 90 秒" | 时长档位切换，重跑 script-generation（这条会滑向 § 2.5） |

#### 定位规则

- 用户说"Hook/开头"→ `role=hook`
- 用户说"论据 N"→ `role=argument_N`
- 用户说"转折"→ `role=turn`
- 用户说"结尾/CTA/引导"→ `role=cta`
- 用户给时间点（"0:35 那段"）→ 按 `segments[].time` 匹配

#### 必须做

- 每次 edit 都 `--edit-note` 明确描述（不是"用户改了下"而是"删除论据 2，其它顺延"）
- **修改逐字稿落盘走 `update --stage script_refining`**，扫描会**自动**在 update 内跑一次并写回 compliance；Agent 只需读返回的 `result.compliance.status` 告诉用户结果即可（v0.1.3 P0-A：扫描从"Agent 动作"降级为"工具内置副作用"）。如果因特殊原因需要**单独**跑一次扫描（例如只想在不改 script 的前提下重新评估），才调 `lite_compliance_scan.py --from-draft <DID> --write-back`

---

## 4 · 元操作意图

### 4.1 `query_status`

| 用户表述 | 举例 |
|---|---|
| "我在干啥" · "现在第几步" · "进度如何" · "刚才弄到哪了" | |

**Agent 动作**：读 `index.json` 找 focus → `show --draft <focus>` → 人话汇报"当前 #A3F 在 outline_refining 阶段，上次改是 30 分钟前，内容是 XXX"

---

### 4.2 `query_history`

| 用户表述 | 举例 |
|---|---|
| "这条改过哪些地方" · "A3F 改动记录" · "history" | |

**Agent 动作**：读 `drafts/active/<uid>/<did>/history.json` → 时间倒序摘要。

---

### 4.3 `help`

| 用户表述 | 举例 |
|---|---|
| "怎么用" · "帮助" · "help" · "你能干嘛" | |

**Agent 动作**：回一个精简版 "我能：①出稿 三段式 ②管 draft ③查进度"。不要贴整个 SKILL.md。

---

### 4.4 `query_market_facts` — **非激活 skill，但必须全量 ingest**（重要）

| 用户表述 | 举例 |
|---|---|
| "今天大盘怎么样" · "拉取今日行情" · "拉取今日热点" · "道指涨跌" · "现在热榜是啥" · "信源有什么" | |

**Agent 动作**：**不激活 streamy-content-gen 三段式**，**不开 Draft**。  
**必须**在 workspace 下用 shell 执行（路径按部署调整）：

```bash
python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --max-items 30
```

- **禁止**只调 `fetch_market.py` / `fetch_hot_rank.py` 拼「半套」答复；**禁止**按用户措辞只拉 `market` 或只拉 `news`。  
- **即使用户只说「今日行情」「今日热点」「快讯」「全量信息」之一**，也**同一动作**：仍跑 **`market,news,social` 全量**，**不得**拆成「先行情一条、再热点一条」或只渲染表格半段。  
- **必须**将返回 JSON 中的 **`markdown_summary` 全文原样**发给用户（可加一行数据来源/时间说明），**不得**自行删减板块、不得把「行情」与「热点」拆成两次不同结构。`markdown_summary` 内含大盘与情绪、六大板块、大事件、全球宏观、热点、社媒、深度内容、中文告警等；**不再**由脚本自动拼接 Tavily 或任何「联网补充」段。大盘块顺序固定为三大指数 → 北向资金 → 其他情绪/资金。  
- **纯拉数命令**：优先 **`query_market_facts.py`**（与 `ingest.py` 同源 JSON，且便于加载 workspace `.env`）；直接调 `ingest.py` 仅用于调试。脚本**不会**写入 `meta.websearch_required` / `meta.websearch_gaps`，也**不会**调用 `tavily-search`。若用户追问某缺口（如北向为 0、社媒为空），可由 Agent **自愿**使用会话内的 WebSearch 在对话中补充说明，且**不得**改写或声称替换了 `markdown_summary` / `sections` 中的 API 数值。  
- 结尾可问一句「要不要就这个开一条稿？」—— 用户说「好」再走 `create_draft`。

---

## 5 · 复合意图拆解

一句话带多个意图时，**按优先级串行执行**：

```
生命周期动作 > 阶段推进 > 内容修改 > 元操作
```

### 5.1 典型复合句

| 用户句子 | 拆解 | 执行顺序 |
|---|---|---|
| "定了 ②，把 Hook 改冲一点" | `confirm_topic(2)` + `edit_content(hook, rewrite)` | 先 confirm 生成大纲 → 再 edit |
| "A3F 丢了，开个聊 AI 的新稿" | `drop_draft(A3F)` + `create_draft(topic=AI)` | 先 drop → 再 create |
| "出逐字稿，60 秒的" | `confirm_outline(time_budget_sec=60)` | 单意图带参数，不拆 |
| "大纲整个重写，更口语化" | `regenerate_stage(outline, tone=口语化)` | 单意图带参数，不拆 |
| "list 一下，然后切到最新那条" | `list_drafts` + `switch_draft(最新 ID)` | 先 list 定位 → 再 switch |
| "这版不行，换一批选题" | `regenerate_topics`（"不行"是情绪不是独立意图，不拆） | 单意图 |

### 5.2 拆解原则

- **不自作主张合并或丢弃次级意图**：用户既然说了就要处理
- **有依赖关系按依赖序**：drop 后才能 create 新稿；switch 后才能在新焦点上 edit
- **执行完第一步再确认第二步**：做完 confirm_topic 生成大纲后，把 edit 在大纲上应用，不要跳过中间结果

---

## 6 · 焦点 Draft 消歧

### 6.1 自动确认规则

| 情况 | 处理 |
|---|---|
| 用户显式带 `#XXX` | 直取 `XXX` |
| 用户未带 ID，`index.json.focus` 存在 | 直取 focus |
| 用户未带 ID，focus 为 null，只有 1 条 active draft | 直取那条 |
| 用户未带 ID，focus 为 null，≥2 条 active draft | **反问** |
| 用户未带 ID，无任何 active draft | 意图若为修改/推进 → 反问"你要改哪条稿？当前没有进行中的稿"；若为 create → 直接走 |

### 6.2 反问话术模板

- **多 draft 歧义**：
  > "你说的是哪条稿？当前有 #A3F（降准历史表现，大纲阶段）和 #B7K（AI 算力，选题阶段）"
- **完全无焦点**：
  > "你想改的是哪条稿？用 `list` 可以看当前所有 draft"
- **意图不明**：
  > "你是想开新稿、改现有稿，还是只是想看今天的市场数据？"

**一次对话最多反问一次**，反问之后用户的回答一定要兜住，不要连续反问。

---

## 7 · "看起来像但不应激活"反例集

Agent 看到这些**不要**走三段式，避免打扰：

| 用户输入 | 正确响应 |
|---|---|
| "帮我写个 Python 脚本" | 走正常代码辅助，不激活本 skill |
| "今天的选股结果发我" | 走 stocky agent，不在本 skill 范围 |
| "上周那条降准的视频数据怎么样" | 功能② 数据复盘，告知未实装 |
| "这个稿合规吗？给我一份全量审计" | 功能③ 深度合规，告知未实装；但**当前 Draft 的逐字稿合规**仍走本 skill 的 lite 扫描 |
| "对标一下 XX 账号" | 功能④，告知未实装 |
| "写个小红书图文" | 独立 skill（未建），告知 |
| "今天大盘 / 热榜是啥" | § 4.4 `query_market_facts`：`query_market_facts.py --sources market,news,social`，**完整** `markdown_summary` 原样展示，不开 draft |
| "你能干嘛" | `help` 意图，给简短介绍 |

---

## 8 · 识别失败的兜底

当 Agent 对本文件所有意图匹配度都不高（置信度阈值由 Agent 本体判断）：

1. **不要硬套**任何意图
2. **回一句澄清**："我不太确定你是想 A（改当前 #A3F 的 Hook）还是 B（开一条新的聊 Hook 的稿）？"
3. **备选项 ≤ 3 个**，别让用户面对长清单
4. 用户澄清后再走对应意图

---

## 9 · 本文件与 SKILL.md 的关系

- `SKILL.md` 说**做什么**（阶段、命令、约束）
- 本文件说**怎么听懂用户要什么**（口语 → 意图）
- 二者矛盾时以 SKILL.md 为准
- 本文件后续扩充只加**新语料 / 新反例**，**不新增意图**（新增意图必须先改 SKILL.md 和 PRD）
