# 领航员内容副驾驶 · 产品现状与路线图（v0.1.4）

> **文档定位**：产品全景视角的阶段总结 + 后续路线图。
> 和 `PRD-streamy-content-gen-v0.1.md` 的关系：PRD 是 v0.1 范围内的需求规格说明，本文件
> 是"产品发展到哪了 / 遇到了什么 / 接下来往哪走"的阶段锚点，随版本迭代。
>
> - **状态**：v0.1.4 hotfix 已落地 + 代码层 smoke 9 步全绿，待飞书第四轮回归验收
> - **作者**：Nathan × streamy Agent
> - **最后更新**：2026-04-22
> - **上游 PRD**：[`PRD-streamy-content-gen-v0.1.md`](./PRD-streamy-content-gen-v0.1.md)（536 行，已冻结）

---

## 目录

1. [产品定位](#1-产品定位)
2. [v0.1 当前能力全景](#2-v01-当前能力全景)
3. [技术架构速览](#3-技术架构速览)
4. [里程碑清单（v0.1.0 → v0.1.4）](#4-里程碑清单v010--v014)
5. [已发现并解决的问题](#5-已发现并解决的问题)
6. [尚未解决的问题 / 已知局限](#6-尚未解决的问题--已知局限)
7. [v2 路线图](#7-v2-路线图)
   - [7.1 数据来源](#71-数据来源重点)
   - [7.2 领航员个性化](#72-领航员个性化重点)
   - [7.3 复盘能力](#73-复盘能力重点)
   - [7.4 v2 其他项](#74-v2-其他项)
8. [v3 路线图](#8-v3-路线图)
9. [风险与依赖](#9-风险与依赖)
10. [附录](#10-附录)

---

## 1. 产品定位

服务于券商 / 理财公司网销线的 **"领航员内容运营副驾驶"**，目标是把"一个领航员（金融顾问）+ 一个内容运营"现在靠个人经验手工出稿的流程，升级成 **"领航员动口 → AI 副驾驶产出 → 运营审核"** 的协作流水线。

完整产品规划包含 4 条主线：

| 功能 | 名字 | 描述 | 现状 |
|---|---|---|---|
| ① | **内容产出**（streamy-content-gen） | 短视频 / 直播的文本产出（选题 → 大纲 → 逐字稿） | ✅ v0.1.4 · 本文重点 |
| ② | **内容复盘** | 已发内容的数据分析与归因 | 🟡 v2 路线中，scope 待业务对齐 |
| ③ | **异常监控** | 合规 / 舆情 / 敏感话题预警 | ⏸ v3 |
| ④ | **对标账号** | 竞品账号拆解与要素迁移 | ⏸ v3 |

**本文覆盖范围**：功能 ① 的产品阶段总结 + 功能 ②③④ 的路线图协同。

---

## 2. v0.1 当前能力全景

### 2.1 用户视角 · 一分钟看懂 v0.1 能做什么

领航员 / 内容运营在 **微信 / 飞书** 群聊里和 streamy agent 自然语言对话，即可完成一条短视频 / 直播脚本从选题到定稿的全流程：

```
用户 ──────────────────── streamy agent（内容副驾驶）
  │
  │ "帮我出一条聊降准的短视频稿"
  │──────────►
  │                                       ① Agent 调 fetch_market / fetch_hot_rank
  │                                          拿到今天的真实数据和热点
  │                                       ② Agent 生成 3 个选题方向（Hook / 数据派 / 反问）
  │◄──────────                               落盘 topic_candidates.json
  │ "我选 ②，Hook 再冲一点"                ③ Agent 打开 Draft #A3F，把 ② 设为 chosen
  │──────────►                              生成大纲 outline.json（含 display 渲染）
  │                                       ④ 用户来回改大纲
  │◄──────────
  │ "出逐字稿"                             ⑤ Agent 生成 segments[]，工具
  │──────────►                              自动渲染 script.md + 自动跑合规扫描
  │ （收到逐字稿 + 合规检查 🟢 通过）       ⑥ 用户继续改或定稿
  │◄──────────
  │ "定稿"                                 ⑦ draft_manager finalize
  │──────────►                              稿子归档到 archive/YYYY-MM-DD/
```

同时支持：

- **多稿并行**：一个用户最多 10 条 Draft/天，用 3 位短 ID（`#A3F`）引用，随时切 / 看 / 放弃
- **跨轮对话记忆**：Draft 状态全落盘，下一轮进来 `list` 就能看到昨天没收尾的稿
- **回退换方向**：大纲 / 逐字稿阶段觉得选题不对，说"换方向"自动回到选题阶段，旧产物被工具自动清理
- **归档查询**：问"我上周那条降准的稿发了吗"一律走 `archive-list` 命令，工具返回真实归档清单

### 2.2 已交付的原子能力

| 能力 | 入口 | 备注 |
|---|---|---|
| 创建 Draft | `draft_manager create --topic` | 返回 3 位短 ID + 初始阶段 + invariants 引导 |
| 列出 active Draft | `draft_manager list --json` | 含 focus 指针 |
| 列出归档 Draft | `draft_manager archive-list --since-days N` | 独立命令，禁止 ls/find 探盘 |
| 查看 Draft 状态 | `draft_manager show --draft` | 阶段 + artifact 清单 + history 摘要 |
| 切换焦点 | `draft_manager switch --draft` | 用户说"继续 #X"走此 |
| 更新阶段产物 | `draft_manager update --draft --stage --payload-file` | 整阶段原子更新 + 自动清 forward + rewind 支持 |
| 原子选定候选 | `draft_manager update --draft --set-chosen N` | 只改 topic_candidates.chosen 字段 |
| 定稿归档 | `draft_manager finalize --draft` | 移到 archive/YYYY-MM-DD/ |
| 放弃归档 | `draft_manager drop --draft --reason` | 同上 + 记 drop 原因 |
| 合规独立扫描 | `lite_compliance_scan.py --from-draft --write-back` | 内嵌在 script_refining，也保留独立入口 |
| 市场数据 | `fetch_market.py --json` | A 股（Tushare）+ 海外（新浪）+ 板块 |
| 公开热榜 | `fetch_hot_rank.py --top 10 --json` | tophub 主源 + HOT_RANK_API_URL 备源 |
| 财联社电报 | `fetch_cls_telegram.py --json` | stub，等 v2 实装 |

### 2.3 Agent 侧产出物（每条 Draft 的完整文件集）

```
drafts/active/<user_id>/<draft_id>/
├── meta.json            Draft 元信息（stage / topic / 时间戳）
├── history.json         所有操作的审计日志（create / update / scan / set_chosen / finalize）
├── topic_candidates.json  选题阶段：3 条候选 + chosen + source_context
├── outline.json + .md   大纲阶段：结构化数据 + 聊天展示
├── script.json + .md    逐字稿阶段：segments[] + 工具自动渲染的 script.md
                          script.json 含 compliance 子字段（scan 自动写回）
```

定稿 / 放弃后整个目录搬到 `drafts/archive/YYYY-MM-DD/<user_id>/<draft_id>/`。

---

## 3. 技术架构速览

### 3.1 三层职责分工

```
┌────────────────────────────────────────────────┐
│  L0 · Agent（ark-code-latest 模型）           │
│     负责：语义理解、意图识别、内容生成、对话渲染  │
└────────────────────────────────────────────────┘
              ↓ 调用 skill 提供的脚本（argparse + JSON）
┌────────────────────────────────────────────────┐
│  L1 · Skill 脚本（streamy-content-gen/scripts）│
│     负责：Draft 生命周期、数据抓取、合规扫描      │
│     原则：零 LLM 调用、全部确定性、原子文件写入     │
└────────────────────────────────────────────────┘
              ↓ 落盘
┌────────────────────────────────────────────────┐
│  L2 · 文件系统（drafts/ + 数据缓存）             │
│     负责：持久化、跨会话对账源、审计轨迹            │
└────────────────────────────────────────────────┘
```

**核心设计原则**（v0.1.2 → v0.1.4 迭代总结出来的）：

> **所有核心数据不变式必须由 L1 工具侧硬编码保证，Agent 自觉遵守只是 bonus。**
>
> 证据：v0.1.2 押宝"prompt 铁律 + Agent 自觉"，5 条 hotfix 有 3 条被 ark-code 绕过；
> v0.1.3 把 3 条下沉到工具侧后通过，但又暴露出新的 3 条 prompt 约束会被绕（阶段跳跃 /
> 无数据源脑补 / 跨会话脑补 DID）；v0.1.4 再下沉，预期稳定。

### 3.2 三段式流水线与工具侧硬约束对照

| 阶段 | Agent 动作 | 工具侧硬约束（v0.1.4 现状） |
|---|---|---|
| **topic_picking** | 读数据 + 生成 3 候选 → `update --stage topic_picking --payload-file` | ① 必须有非空 `source_context[]` 或 evidence_anchor，否则 `CANDIDATES_REQUIRE_DATA_SOURCES` <br> ② 黑名单词（brainstorm/llm/脑补）全命中 → 拒收 <br> ③ 选定候选只能走 `--set-chosen N`，edit 直写会撞下一步 update 校验 |
| **outline_refining** | 基于 chosen 生成大纲 → `update --stage outline_refining` | 阶段跳跃硬禁：topic_picking 不能直接跳 script_refining，返 `STAGE_SKIP_FORBIDDEN` + `expected_next_stage` |
| **script_refining** | 生成 segments[] JSON → `update --stage script_refining` | ① script.md 由工具从 segments[] 按固定模板渲染（Agent 不写 markdown） <br> ② 合规扫描内嵌（Agent 不用单独调）<br> ③ payload 里的 display_markdown 被忽略 + 返 deprecation_warnings |
| **finalized / dropped** | `finalize` / `drop` | 所有 mutating 命令前置校验 Draft ID 在真实 active 列表 → `DRAFT_NOT_FOUND_IN_SESSION` + 列出真实 DID |

### 3.3 可移植约束的两层封装（v0.1.4 新增）

为了让 skill 跨 agent / 跨 workspace 使用时约束也跟着走（不绑定 streamy-agent-specific 的 AGENTS.md / SOUL.md）：

- **L1（SKILL.md 顶部 preflight-rules）**：任何 agent 读 SKILL.md 时都会看到的 6 条精简铁律（阶段逐级 / 数据源必需 / 时效词必抓数据 / DID 不脑补 / drafts 不 edit / 必读 invariants）
- **L2（tool result 的 `invariants[]` 字段）**：每次调 `create / update / switch / set-chosen` 工具，返回体里都带当前阶段的 invariants 数组——Agent 即使没读 SKILL.md，看 result 就会被提醒

这两层封装跟着 skill 代码走，不依赖具体 agent 或 workspace 的 system prompt 配置。

---

## 4. 里程碑清单（v0.1.0 → v0.1.4）

| 版本 | 日期 | 核心动作 | 验收 |
|---|---|---|---|
| **v0.1.0** | 2026-04-21 | 骨架就位：draft_manager 7 子命令 / 3 个核心 prompt（Thick 版）/ 4 个数据脚本 / SKILL.md Playbook | Cursor 本地 dry-run 5 路径 ✅ |
| **v0.1.1** | 2026-04-22 | 飞书首次接入 hotfix：`_common.get_workspace_root` 加 `__file__` 回溯 fallback，挡住 Agent cd 后 cwd 污染 | — |
| **v0.1.2** | 2026-04-22 上午 | 飞书二轮验收 5/5 通过后 hotfix：`--write-back` 合规回写 + `{pending}` 占位符去除 + `list --include-archive` + meta.topic rewind 修复 + NLU rewind 消歧 | 代码层 6 step smoke ✅ |
| **v0.1.3** | 2026-04-22 下午 | v0.1.2 prompt 约束 3/5 被 Agent 绕过，全面工具侧硬约束补强：`update script_refining` 内嵌合规扫描 / script.md 工具渲染 / `--set-chosen N` 单字段 patch / `archive-list` 独立命令 / 时效词铁律 / `#<DID>` 首行展示 / drafts 路径声明 / meta.topic 语义重定义 | 代码层 8 step smoke ✅ |
| **v0.1.4** | 2026-04-22 晚 | v0.1.3 飞书三轮路径 1 一条消息就崩（跳阶段 + 无数据源 + 脑补旧 DID），再下沉 3 条到工具侧硬编码 + 可移植封装两层（preflight-rules + invariants[]）；V4 物理拦截降级为已知局限 | 代码层 9 step smoke ✅ · **待飞书四轮** |

**累计代码体量**（v0.1.4）：

```
streamy-content-gen/
├── scripts/          ~1,800 行 Python（draft_manager 主力，_common 共享，
│                               script_renderer / lite_compliance_scan 各约 150 行）
├── prompts/          ~2,000 行 Markdown（topic/outline/script-generation Thick 版 + intent 映射）
├── templates/        ~200 行（script.schema.json + md 展示骨架）
├── references/       ~500 行（行为规范）
├── data/compliance/  ~80 行黑词表（业务方后续替换）
└── SKILL.md + README.md + _meta.json  ~600 行
```

---

## 5. 已发现并解决的问题

按严重程度分三类：

### 5.1 P0 · 数据一致性（全部下沉工具侧解决）

| 编号 | 问题 | 修复版本 | 解决方案 |
|---|---|---|---|
| v0.1.2-#1 | Agent 用 `edit` 硬改 `script.json` 的 compliance 字段（3 次复现） | v0.1.2 | `lite_compliance_scan --write-back` + SKILL.md 写入白名单 |
| v0.1.2-#2 | `script.md` 里固化了 `合规扫描：{pending}` 占位符 | v0.1.2 | prompt 模板删除 + 合规状态只走对话实时展示 |
| v0.1.2-#3 | Agent 列归档时用 `find` / `ls`，topic 字段脑补 | v0.1.2 | `list --include-archive` 入口 |
| v0.1.3-#1 | v0.1.2-#1 复发：Agent 漏带 `--write-back` | v0.1.3 | `update --stage script_refining` 内嵌 scan |
| v0.1.3-#2 | v0.1.2-#2 复发：Agent 凭记忆把 `{pending}` 又写回 display_markdown | v0.1.3 | script.md 由工具从 segments[] 自动渲染 |
| v0.1.3-#3 | Agent 用 `edit` 硬改 `topic_candidates.json.chosen` | v0.1.3 | `--set-chosen N` 单字段原子 patch |
| v0.1.3-#4 | Agent 列归档时凭对话历史捞出不存在的 ID（#KWJ/#3L6/#QJT） | v0.1.3 | `archive-list` 独立命令 + intent.md 最严禁令 |
| v0.1.4-#1 | Agent 从 `create` 直接 `update script_refining`，跳过 topic/outline 两阶段 | v0.1.4 | `assert_stage_transition` 硬禁跨 forward |
| v0.1.4-#2 | 时效主题（"最近 A 股"），Agent 不抓数据直接凭训练知识脑补候选 | v0.1.4 | `assert_candidates_have_data_sources` 强制 source_context + 黑名单拦截 |
| v0.1.4-#3 | Agent 引用上轮会话里已 drop 的老 Draft ID（`#X2E`） | v0.1.4 | 所有 mutating 命令前置 `assert_draft_in_session` + 返回真实 active_drafts |

### 5.2 P1 · 体验 / 行为一致性

| 编号 | 问题 | 修复版本 | 解决方案 |
|---|---|---|---|
| v0.1.3-P1-E | 时效主题不调数据 | v0.1.3 | SKILL.md 铁律 11（v0.1.4 降为 L2 硬拦截兜底） |
| v0.1.3-P1-F | create 后 Agent 首行不写 `#<DID>` | v0.1.3 | intent.md 硬规则 + SKILL.md 铁律 13（v0.1.4 Q3 降到 P3 暂缓） |
| v0.1.3-P1-G | Agent 怀疑 drafts 路径，想用 shell 探盘 | v0.1.3 | SKILL.md 铁律 12 明确 `$WORKSPACE_ROOT/drafts/` 不变式 |

### 5.3 P2 · 语义一致性

| 编号 | 问题 | 修复版本 | 解决方案 |
|---|---|---|---|
| v0.1.3-P2-H | `meta.topic` 在进入 outline/script 后被 payload.title 覆盖，语义与 topic_candidates 对不上 | v0.1.3 | 重新定义 meta.topic = 只在 topic_picking 刷新 |
| α-修复-A | SKILL.md §4 异常兜底字段名与 fetch_market 实际契约不一致 | α 修复 | 对齐字段名 |
| α-修复-B | rewind 时 forward artifact 滞留磁盘，show 回显错位 | α 修复 | `update` 自动清 `outline.*/script.*` + 回填 `cleaned_forward` |
| α-修复-C | topic 阶段 `meta.topic` 取值歧义 | α 修复 | 优先级链：`context_used.topic` > `candidates[0].title` |

---

## 6. 尚未解决的问题 / 已知局限

### 6.1 V4 · 物理拦截 edit/write → drafts/**/*.json ⚠️

**问题**：即便有 SKILL.md 铁律 + invariants 提醒 + 下一步 update 撞墙，Agent 仍可能用 `edit` / `write` 工具直接改 drafts 下的 JSON 文件（v0.1.2 / v0.1.3 测试中都复现过）。理想状态是在 Agent 调 `edit` 时物理拦截掉。

**调研结论**：OpenClaw runtime 的 hook 系统只支持 `command:new/reset/stop` / `agent:bootstrap` / `gateway:startup` / `tool_result_persist` 等事件，**没有** `preToolUse` / `beforeToolCall` 这种能 deny 工具调用的入口。而 `write` / `edit` 是 ark-code provider 自带的工具，绕过 OpenClaw 中间层。Cursor IDE 项目级 `.cursor/hooks.json` 格式不适用 OpenClaw 场景。

**当前降级方案（多层软防御）**：
- L1：SKILL.md 顶部 preflight-rules 标记 drafts 为 tool-owned
- L2：每次 tool result 的 invariants[] 里明说"禁止 edit drafts"
- L3：如果 Agent 偷改了，下一次 `update` 的 stage / schema / session 校验撞墙（例如直改 chosen=2，下一次 `--set-chosen 3` 时会把 `prev_chosen=2` 记到 history，留下审计痕迹）

**可能的上游路径（v2 候选）**：
1. 等 OpenClaw 官方增加 `preToolUse` / `beforeToolCall` 事件
2. 用 `tool_result_persist` hook 做事后回滚（write 成功后校验 path 命中 drafts/**/*.json → 读出备份内容覆盖回去 + 告诉 Agent 失败）
3. 把 drafts/ 放到只读挂载点 + 用 FUSE 或类似机制做 path 级 allowlist（系统工程量大，ROI 低）
4. ark-code provider 层能否配置工具黑名单（需上游支持）

### 6.2 飞书端 v0.1.4 第四轮回归验收未跑 🟡

v0.1.4 代码层 smoke 9 步全绿，但 ark-code 在真实飞书环境的"撞墙后如何读 invariants[] 重新规划"这个二阶行为是**新变量**。需要跑 5 路径回归才能判定：

- [ ] 路径 1（Happy）：时效主题 → Agent 是否正常调 fetch_hot_rank / fetch_market
- [ ] 路径 2（合规告警）：compliance 命中后 Agent 的修改流程
- [ ] 路径 3（rewind）：换方向场景，forward artifacts 清理
- [ ] 路径 4（多稿切换）：focus 消歧
- [ ] 路径 5（归档查询 + 错误 ID）：archive-list 独立命令是否被正确使用

### 6.3 `user_id` 网关注入未实装 🟡

`OPENCLAW_USER_ID` 环境变量当前未由 openclaw 网关注入，所有 Draft 归到 `default` 用户。多领航员同时使用会串 Draft，但 v1 约定只有 Nathan 单人试用，不阻塞。v2 开放给多个领航员前必须解决。

### 6.4 CTA 话术占位 🟡

`references/cta-patterns.md` 只放了 3 条通用占位（"点关注"/"评论区聊"/"主页有干货"），业务方真实加粉话术、导资话术、合规要求（是否能出具体产品名？是否能报收益率？）未对齐。需业务方在 v1 交付后补充或在 v2 接入合规词库时一并处理。

### 6.5 财联社电报 stub 🟡

`fetch_cls_telegram.py` 是带 `schema_preview` 字段的 stub，实际永远返回 `items: []`。原因是财联社 API 需要授权 token，v1 未申请到。等 v2 申请或改用可替代源（东方财富电报 / 金十数据）。

### 6.6 合规扫描精度有限 🟡

v1 的 `lite_compliance_scan.py` 只做字面量 + `re:` 前缀正则匹配，比如黑词表里写 "承诺" 会把 "不承诺任何收益" 也扫出来。**只告警不改写**的设计兜住了这个问题，但在 v2 业务词库替换后误报率可能上升。需要业务方审视词库时引入上下文白名单（例："不 + 承诺" / "非 + 保证" 这种否定前缀排除）。

### 6.7 内容质量瓶颈 🟡

v1 明确定位"骨架优先，内容质量达通用财经 LLM 水平即可"。当前产出的文案：

- 结构正确（Hook / 论据 / 转折 / CTA 齐全）
- 数据引用真实（Tushare / 热榜）
- 但"像任意一个 AI 写的"，**没有领航员的口癖和价值观**

这是 v2 个性化要解决的核心问题（见 §7.2）。

---

## 7. v2 路线图

**v2 定位**：从"能用的骨架"进化到 **"像 Navigator 自己说出来的内容"** + **"数据驱动的持续优化闭环"**。

优先级：**数据来源 ≈ 领航员个性化 > 复盘能力**（但三者是强耦合的，复盘能力所需要的数据接入和个性化的语料反哺都要同步开工）。

### 7.1 数据来源（重点）

**痛点**：v1 的数据源偏"公开轻量"，无法支撑精细财经叙事，且部分关键源是 stub。

#### 7.1.1 现状

| 数据源 | v1 状态 | 覆盖 | 缺口 |
|---|---|---|---|
| Tushare | ✅ 实装 | A 股指数 / 北向 / 行业 | Token 要用户自己配；无多用户 quota 管理 |
| 新浪财经 | ✅ 实装 | 道指 / 标普 / 纳指 / VXX / 金 / 油 | 无欧股 / 日股 / 港股 |
| 公开热榜（tophub.today） | ✅ 实装 | 微博 / 百度 / 知乎 / 36kr 等 8 榜单 | HTML 抓取不稳定，站点变动会挂 |
| 财联社电报 | 🟡 stub | — | 授权 token 未申请 |
| 万得 / 东方财富 / 金色财经 | ❌ | — | 未接入 |
| 研报摘要 | ❌ | — | 未接入，且版权争议大 |
| 公司公告 / 交易所披露 | ❌ | — | 未接入 |

#### 7.1.2 v2 目标

| # | 能力 | 设计要点 |
|---|---|---|
| D1 | **Tushare Pro 网关化** | 公司统一 Pro token，skill 层加 per-user quota 管理 + rate limit，领航员无感接入 |
| D2 | **财联社电报实装** | 申请官方 API 或改走金十数据 / 华尔街见闻；统一 `cls_telegram.json` schema（从 stub 平滑升级） |
| D3 | **万得 / 东方财富补全** | 覆盖港股 + 欧股 + 日股、公司公告、北向细分流向、主力资金流 |
| D4 | **研报摘要池** | 接入券商研报聚合（Wind / iFinD 数据权限），只抓"标题 + 一句话摘要 + 评级"，不抓正文（规避版权） |
| D5 | **数据摘要层** | `fetch_market` 不直接返原始数字，而是返"可直接进 prompt 的叙事摘要"（如："今日 A 股三大指数上涨，沪深 300 +0.8%，领涨板块为银行、煤炭，符合防御性风格"）—— 降低 Agent 的数据解读负担，提升内容一致性 |
| D6 | **数据源版权 / 合规审计** | 每次抓数据记录 source + license + 抓取时间，后续若被下游业务问"这数据哪来的能不能用" 有据可查 |
| D7 | **热榜源扩充 + 容灾** | 增加金色财经、雪球热议、微博财经榜；tophub 挂掉时 fallback 到 API 源 + 再 fallback 到"最近缓存 + 标注数据时效" |

#### 7.1.3 v2 数据源架构草图

```
┌────────────────────────────────────────────────┐
│  统一抽象层 fetch_*                             │
│    ├── fetch_market.py   行情（Tushare+新浪+Wind） │
│    ├── fetch_hot_rank.py 热榜（tophub+金色+雪球）  │
│    ├── fetch_telegram.py 电报（财联社+金十+华尔街见闻） │
│    ├── fetch_research.py 研报摘要（券商聚合）      │
│    └── fetch_disclosure.py 公告（交易所+上市公司）  │
│         ↓
│  数据摘要层 summarize_*（v2 新增）                │
│    ├── summarize_market.py  → 叙事摘要            │
│    ├── summarize_hot.py     → 热点主题卡          │
│    └── summarize_research.py → 观点立场卡          │
│         ↓
│  统一 schema + 缓存 + 审计                      │
└────────────────────────────────────────────────┘
```

### 7.2 领航员个性化（重点）

**痛点**：v1 产出的文案"像任意一个 AI 写的"，领航员 A 和领航员 B 拿到同一个 topic 会出几乎一样的稿。这让 AI 副驾驶只是"省打字时间"，没有沉淀领航员的专业价值。

#### 7.2.1 现状

- ❌ 无 persona 建模
- ❌ 无领航员语料库
- ❌ 无差异化 prompt
- ❌ 无 CTA 话术分档

#### 7.2.2 v2 目标

**能力 P1：Persona 建模**

| 维度 | 字段 | 来源 |
|---|---|---|
| 身份 | 姓名、机构、擅长领域、从业年限 | 领航员手工录入 |
| 语言风格 | 口癖、开场白、结束语、常用比喻、语速偏好（快 / 稳 / 慢） | 语料分析 + 领航员校对 |
| 价值观 | 风险偏好（激进 / 稳健）、对"追涨" / "抄底"的态度、底线 | 访谈式录入 |
| 内容偏好 | 常讲赛道、避讲话题、数据深度、是否带情绪 | 语料分析 |
| 合规约束 | 机构合规要求、禁用词（叠加在公司黑词表上） | 合规部录入 |
| CTA 话术 | 加微 / 导资 / 引流 3 档话术模板 | 运营部录入 |

每个 Draft 绑 `persona_id`，`create` 时自动读取 focus 领航员的 persona 注入 prompt。

**能力 P2：语料反哺**

领航员历史视频字幕 / 社群对话 / 公众号文章作为 few-shot 样本注入 topic-generation / outline-generation / script-generation prompt 开头。按内容匹配度选 top-k（RAG 思路），不是整个语料库全塞。

**能力 P3：A/B 输出对比**

同一大纲跑两种 persona 风格 / 两种 Hook 类型，让领航员选，**选择本身反哺语料库**（隐式 RLHF，不需要显式打分）。

**能力 P4：合规词库分层**

```
合规黑词 = 国家通用（证监会 / 基金业协会）
         + 机构通用（券商 / 理财公司内规）
         + persona 个性化（领航员对自己 IP 的额外限制，如不提特定竞品）
```

v1 的 `data/compliance/blacklist-common.txt` 升级到 `compliance/{common, org, persona}/` 三层叠加。

**能力 P5：Draft → Persona 反馈回路（和复盘耦合）**

当功能② 复盘数据回来说"你的 Hook 用'XX'开头完播率低"，系统把这条规则反哺到 persona 的"避用 Hook 模板"列表，下次生成时避免。

#### 7.2.3 v2 数据模型增量

```
workspace-streamy/
├── personas/                 ← v2 新增
│   └── <persona_id>/
│       ├── profile.json       基础档案
│       ├── voice-samples/     语料样本（MD 或字幕）
│       ├── cta-patterns.md   个性化 CTA
│       ├── avoid-rules.json  避用规则（来自复盘反馈）
│       └── compliance-extra.txt 个性化合规黑词
└── drafts/active/<uid>/<did>/
    └── meta.json              新增 persona_id 字段
```

### 7.3 复盘能力（重点）

**痛点**：v1 产出的稿子一旦发布就脱离 skill 视野，没人告诉 AI 副驾驶"你上周生成的那条降准稿播放量 5k 完播率 18%"。没有反馈就没有改进。

这是功能 ② 的核心职责，但和功能 ① 强耦合——复盘的归因一定要能回溯到 Draft 的 topic / outline / script 级别。所以复盘 skill 设计时，Draft 的结构化落盘就是"埋点"。

#### 7.3.1 现状

- ❌ 无数据接入（视频平台 API 未接入）
- ❌ 无归因能力
- ✅ 但数据基础在——每条 Draft 的完整 history + artifact 落盘，为复盘提供了"原材料"

#### 7.3.2 v2 目标

**能力 R1：视频平台数据接入**

| 平台 | 关键指标 | 接入方式 |
|---|---|---|
| 抖音 / 快手 | 播放量、完播率、点赞率、评论率、转发率、粉丝增量、私信转化 | 开放平台 API（需企业号授权）或创作者后台爬 |
| 视频号 | 同上 + 朋友圈转发率 | 视频号助手 API |
| 小红书 | 曝光、阅读、收藏、评论 | 创作者后台 API |
| B 站 | 同抖音 + 弹幕量 | 创作者中心 API |

**能力 R2：Draft ↔ 真实视频关联**

在 `finalize` 时让领航员回填"发布地址 / 视频 ID"，或用标题 / 首句 Hook 做模糊匹配：

```
draft_manager finalize --draft A3F --published-at https://... --platform douyin
```

finalize 时自动建 `published.json` 记录，之后数据拉回来有 join key。

**能力 R3：多维归因**

```
一条高表现视频 → 回溯：
   - 选题类型（反直觉钩子 / 数据派 / 反问派 / …）
   - 选用的数据源（tushare:index_daily / tophub:weibo / …）
   - Hook 长度（秒数 + 字数）
   - 视频时长档（30s / 60s / 90s）
   - persona 版本
   - 发布时间段
   - 合规扫描 warn 数
```

所有维度都从 Draft 结构化数据里直接取，不需要二次打标。

**能力 R4：模式挖掘**

- **Hook 类型 × 完播率**：哪类开场留人最好
- **时长 × 完播率**：用户侧实际能看完多长
- **主题类别 × 加微率**：哪类主题最能沉淀私域
- **发布时间 × 播放量**：黄金时段识别

挖掘产物有两种用法：
1. 反哺 topic-generation / script-generation 的 few-shot，提升未来生成质量
2. 给领航员出 weekly report："本周 top 3 / bottom 3 / 建议下周多写 X 类"

**能力 R5：反哺 persona 和 prompt**

```
复盘发现："你用'今天聊个大事'开头的视频完播率比均值低 15%"
   ↓
自动写入 persona.avoid-rules.json: {"hook_patterns_to_avoid": ["今天聊个大事"]}
   ↓
下次生成时 topic-generation prompt 的 few-shot 自动回避这类 Hook
```

**能力 R6：领航员 dashboard**

飞书 / 微信 bot 支持自然语言问：
- "本周我发了几条？"
- "本月加粉最好的一条是哪个？"
- "我和 @张三 的完播率差距在哪？"

skill 脚本聚合 `drafts/archive/` + `published.json` + 视频平台 API 数据回答。

#### 7.3.3 复盘 skill 的独立性

功能② 复盘 skill（暂定 skill name：`streamy-content-review`）是**独立 skill**，但消费功能① 的产出：

```
streamy-content-gen (产出)
     ├── 写 drafts/active/...  (内容 artifact)
     └── 写 drafts/archive/... (已归档)
            ↓ 作为只读输入
streamy-content-review (复盘)
     ├── 读 drafts/archive/**
     ├── 读 personas/
     ├── 拉视频平台 API 数据
     ├── 写 reviews/<published_id>/analysis.json
     └── 写 reports/weekly/<YYYY-WW>.md
            ↓ 反哺
streamy-content-gen (下一轮生成)
     └── 读 personas/<pid>/avoid-rules.json 注入 prompt
```

两个 skill 在同一 workspace 下协作，drafts/ 目录是"共享内存"。复盘 skill 对 drafts/ 只读，不写。

### 7.4 v2 其他项

| 项 | 描述 |
|---|---|
| E1 · `OPENCLAW_USER_ID` 网关注入 | 开放多领航员协作的前置条件 |
| E2 · `update --mode merge` 真增量 | CTA 局部改现在要重写整个 script.json，`merge` 模式只改特定字段 |
| E3 · Draft Fork | 支持"A/B 两版并行"场景（v1 砍掉的） |
| E4 · 多端推送 | 稿子定稿后可一键推到企业微信审核 / 发送邮件提醒剪辑 |
| E5 · 草稿 TTL 管理 | 超过 30 天未动的 active 草稿自动 drop，防止 active 列表膨胀 |

---

## 8. v3 路线图

**v3 定位**：从"单用户副驾"进化到 **"内容运营中台"**。

此时 ① ② 已经成熟，③ ④ 开工。

### 8.1 功能 ③ · 异常监控

| 能力 | 描述 |
|---|---|
| 实时舆情监控 | 持续扫公开热榜 + 财经媒体，命中"敏感主题 / 突发事件"时主动推送给领航员："某某公司 XX，建议今天调整选题" |
| 深度合规引擎 | 不再是 v1 的字面量黑词表，接入专业合规 NLP（如清律、天眼通等）做上下文合规判断 |
| 事前预警 | 在 `script_refining` 阶段检测到"触及当前舆情禁区"时硬拦截（不只是 warn） |
| 事后追踪 | 已发视频的评论区舆情反扫，命中风险时自动提醒领航员删除 / 添加免责声明 |

### 8.2 功能 ④ · 对标账号

| 能力 | 描述 |
|---|---|
| 对标账号库 | 领航员维护自己的"对标领航员清单"（3-5 个行业标杆） |
| 视频拆解 | 抓对标账号最新爆款，自动拆解"Hook 类型 / 时长 / 叙事节奏 / CTA 形式" |
| 可迁移要素标记 | 哪些元素可以学、哪些因为价值观或合规不能学 |
| 灵感注入 | 生成新 Draft 时，可选"参考 XX 账号的 YY 风格"，对标要素注入 prompt |

### 8.3 跨平台适配

同一大纲 → 针对不同平台生成差异化文案：

| 平台 | 特点 | 文案差异 |
|---|---|---|
| 抖音 | 节奏快、前 3s 留人 | 重 Hook、短句、强情绪 |
| 视频号 | 微信生态、信任感强 | 偏理性、强身份背书、引 CTA 加微 |
| 小红书 | 图文混合、种草属性 | 强"干货清单"、多数据点、emoji |
| B 站 | 长视频、逻辑深 | 拉长到 3-5 分钟、加 timeline、引弹幕 |

### 8.4 团队管理与规模化

- 多领航员并行使用（不再只是 Nathan 一人）
- 团队级 dashboard（运营总监视角看所有领航员的产量、质量、加粉贡献）
- Persona 共享与迭代（集团总部下发"机构话术规范"，所有 persona 强制叠加）

### 8.5 内容中台化

内容生产与分发的闭环全部由 skill 编排：

```
热点监控（③）触发选题 → content-gen 生成 → 多平台适配（⑤）
     → 合规审核（③） → 发布 → 数据回流 → 复盘（②） → persona 迭代
          ↻ 反哺下一轮内容生产
```

---

## 9. 风险与依赖

| 类别 | 风险 | 缓解 |
|---|---|---|
| 技术 | ark-code 对 invariants[] 的读取率不确定 | 飞书四轮验收观察；如读取率 <50% 考虑加醒目的 `[must-read]` 标签 |
| 技术 | OpenClaw runtime 未来是否会支持 preToolUse | 关注上游，v2 如开放则立即启用 V4 物理拦截 |
| 技术 | Tushare Pro 费用随用户数增加 | v2 设计时用 per-user quota + 本地缓存复用 |
| 数据 | 视频平台 API 授权门槛（企业号 / 创作者等级） | 先从领航员自有账号申请，逐步推广 |
| 数据 | 研报版权 | 只抓摘要不抓正文，或只用公开可引用部分 |
| 合规 | 深度合规引擎的供应商选型 | 和合规部早期对齐，不晚于 v2 中期决策 |
| 业务 | 领航员愿意回填"发布视频地址"吗？ | v2 设计"半自动匹配"+ 未匹配上的不强求 |
| 业务 | Persona 建模所需访谈量 | 先从领航员主动的自描述 + 语料自动抽取开始，低参与度起步 |
| 组织 | 功能 ② 的 scope 与业务方的期望对齐 | **当前状态：Nathan 暂停中，待和业务对齐后再 kickoff** |

---

## 10. 附录

### 10.1 版本术语速查

| 术语 | 含义 |
|---|---|
| **Draft** | 一条视频 / 直播稿的完整生命周期单元，用 3 位短 ID（`#A3F`）引用 |
| **Stage** | Draft 当前所处阶段：`topic_picking` → `outline_refining` → `script_refining` → `finalized` / `dropped` |
| **Rewind** | 从后面的阶段回到前面的阶段（如 script_refining → topic_picking），自动清 forward artifacts |
| **invariants[]** | tool result 里的硬约束清单字段（v0.1.4 引入），告诉 Agent 当前阶段允许和禁止的操作 |
| **preflight-rules** | SKILL.md 顶部注释块（v0.1.4 引入），跨 agent 可移植的 6 条精简铁律 |
| **工具侧硬约束** | 核心数据不变式由 Python 脚本硬编码保证，不依赖 Agent 读 prompt 自觉执行 |
| **Persona（v2）** | 领航员个性化档案：身份 + 语言风格 + 价值观 + CTA 偏好 + 合规叠加 |

### 10.2 关键文件索引

| 文件 | 作用 |
|---|---|
| `streamy-content-gen/SKILL.md` | Agent 的主入口 Playbook，含 preflight-rules 和 17 条铁律 |
| `streamy-content-gen/README.md` | 开发者视角的部署指南 + 全版本 changelog |
| `streamy-content-gen/_meta.json` | skill 版本元数据 |
| `streamy-content-gen/prompts/` | Agent 生成内容的 prompt 模板（topic / outline / script / intent） |
| `streamy-content-gen/scripts/draft_manager.py` | Draft CRUD 主力，所有核心硬约束在这 |
| `streamy-content-gen/scripts/_common.py` | 共享工具库，含 `assert_*` 校验族 + `compute_invariants` |
| `streamy-content-gen/scripts/script_renderer.py` | script.md 自动渲染器（v0.1.3 引入） |
| `streamy-content-gen/templates/script.schema.json` | 逐字稿 JSON schema |
| `streamy-content-gen/data/compliance/blacklist-common.txt` | v1 占位黑词表，业务方替换 |
| `docs/PRD-streamy-content-gen-v0.1.md` | v0.1 需求规格（536 行，已冻结） |
| `docs/PRODUCT-STATUS-v0.1.4.md` | 本文档 |
| `/root/.openclaw/MEMORY.md` | Coding agent 的跨会话长期记忆（Nathan 可读，含所有决策轨迹） |

### 10.3 验收历史

| 轮次 | 日期 | 版本 | 结果 |
|---|---|---|---|
| 1 | 2026-04-22 上午 | v0.1.1 | 飞书首通，发现 cwd 污染问题 → v0.1.2 修 |
| 2 | 2026-04-22 上午 | v0.1.2 | 5/5 通过但 3 条 prompt 约束被 Agent 绕过 → v0.1.3 修 |
| 3 | 2026-04-22 下午 | v0.1.3 | 路径 1 一条消息就发现 3 个新盲点 → v0.1.4 修 |
| 4 | **待跑** | **v0.1.4** | **期望 5/5 通过；重点观察 invariants[] 读取率** |

### 10.4 下一步 immediate action

1. **飞书端 v0.1.4 第四轮回归验收**（5 路径）——由 Nathan 主持，skill 侧挂 monitor 观察
2. 验收通过后 → v0.1.x 功能 ① 封板，移出"开发态"进入"维护态"
3. 与业务方对齐功能 ② 复盘 skill 的 scope → 启动 v2 PRD 收敛
4. **与此同时**，数据源 v2 接入（D1 ~ D7）可独立于 ② 并行推进，不阻塞业务对齐

---

**文档版本**：1.0 · 2026-04-22
**下一次更新触发点**：v0.1.4 飞书四轮验收结果 / 业务方确认 ② 复盘 scope / v0.2 kickoff
