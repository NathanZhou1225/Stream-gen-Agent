# Workflow 门禁规则（详细版）

> 本文件为 `MEMORY_workflow.md` 的碎片，存放完整的硬门禁规则。**仅在进入具体开稿阶段时按需 read**，避免每轮全量加载。

---

## 5.0 开稿前置流程（硬门禁）

每次内容生成必须按顺序执行，不得跳步、遗漏。每步完成后打勾确认：

✅ **门禁0：user-style 绑定检查**
进入 `outline_refining` 前必须完成：
- [ ] 已调用 `user_style_manager list` 拉取所有可用风格
- [ ] 已向用户展示并明确询问选择
- [ ] 已收到用户明确答复（不得用"默认风格"跳过）
- [ ] 已将 `style_id` 绑定到 draft

✅ **步骤1**：用户仅要盘面事实 → `MEMORY_ingest.md` 全量 ingest + `markdown_summary` 原样展示；用户进入开稿链 → preflight + topic_payload

✅ **步骤2**：`draft_manager update --stage topic_picking` → 展示三候选（标题 + thesis + 3 论据）

✅ **步骤3**：用户选定后 `draft_manager update --apply-topic-choice <N>`（一次写入 chosen + evidence_pack，无需二次 preflight；**禁止**默认加 `preflight --allow-targeted-fetch`，证据不足见 `source_gaps`）

✅ **步骤3A**：证据包落盘后、进入 outline 前，**一次**确认稿件类型 + 风格：`helper list-profile-options` → 用户选组合 → `helper bind-profile`（**禁止只问风格**）

✅ **步骤4**：`content_type` 与 `style_id` 均已绑定后进入「大纲→逐字稿→定稿」

---

## 5.0A 明确方向优先（硬规则）

- 用户首句已给开稿方向 → **禁止**先走"通用热点兜圈子"
- 必须直接执行：`preflight --direction "<用户原话>" → topic_picking → 用户选候选 → 证据包 → list-profile-options → bind-profile → outline`
- 方向词必须原样进入 `topic_payload.direction`
- 用户说"先看盘 / 只要事实" → 按 `MEMORY_ingest.md` ingest，不得用旧五段式替代

---

## 5.0B 大纲生成前置校验

- [ ] 已绑定 `content_type`（未绑定则 `CONTENT_TYPE_REQUIRED_BEFORE_OUTLINE`）
- [ ] 已绑定 `style_id`，未绑定则不得生成大纲
- [ ] payload 路径为 `drafts/.../<DID>/_scratch/outline_payload.json`，**禁止** `/tmp/outline_*`
- [ ] 已读取 `style_context`（结构偏好 + 语气 + 禁忌）
- [ ] 大纲 `points[]` 数量符合 `content_type`（如 `market_view` ≥5），不得仅用通用 Hook/方法论/CTA 三段式替代模板

---

## 5.0C 安全批量 helper

- `start-topic --direction "<方向>"` → 创建 Draft + preflight + topic_picking → 返回三候选 → **必须停下等用户选**
- `apply-choice --draft <DID> --candidate-id <N>` → 证据包落盘 → `list-profile-options` 展示类型+风格 → 等用户一次确认
- 禁止从创建到定稿全自动；候选、证据包、风格、定稿为用户决策节点

---

## 5.1 稿件追踪标识

- 展示选题/成稿/状态时必须标注 `#<DID>`
- 每次重要操作前醒目显示 Draft ID

---

## 5.2 成稿收尾流程

- 成稿后必须询问：
  1. 是否确认定稿归档？
  2. 是否执行 auto-refine？
- 不得遗漏

---

## 5.3 异常处理

- `TOPIC_CANDIDATES_EMPTY` → 手动构建 `candidates[]`（至少 3 条）后重试

---

## 5.4 选题展示硬规则（飞书）

- **禁止**只给"标题 + 切入角度"
- **必须**展示：标题 + thesis + 3 条 evidence
- 论据必须与用户方向同域

---

## 5.5 飞书呈现去工程化

- **禁止**：多条短消息直播内部步骤、复述 CLI 路径、贴英文报错
- **允许**：单条消息交付里程碑产物（证据包、大纲、逐字稿）
- 稿件类型用中文（大盘观点 / 投教 / 人设介绍）；slug 仅在工具参数

---

## 5.6 逐字稿模板匹配（按 content_type）

- `script_refining` 必须根据 `meta.json` 的 `content_type` 读取对应模板
- segments 结构严格按模板段序填充
- `production_appendix` 四块（camera_shots / stickers_effects / visual_assets / host_actions）每块 3-5 条

**三种模板核心结构**：

| 类型 | 结构 | 核心特征 |
|------|------|----------|
| `market_view` | 开篇锁客 → 导流 → 亮剑 → 论证 → 总结 → CTA | 情绪炸弹 + 圈子价值 |
| `investor_edu` | 痛点 → 引导 → 干货 → 深化 → 钩子 | 痛点唤醒 + 方法论 |
| `persona_intro` | 钩子 → 价值 → 人设 → 权益 → 号召 | 战绩数据 + 从业背景 |

---

## CLI 参考（技术细节）

- `draft_manager schema --stage outline_refining|script_refining --draft <DID> --inject-prompt-template --json`（一次拉齐 schema + 稿件类型）
- `draft_manager update --stage outline_refining|script_refining --payload-file <json> --validate-only --json`（预检；payload **禁止**放 `/tmp`，用 `drafts/.../<DID>/_scratch/`）
- `draft_manager update --apply-topic-choice <N>`（证据包落盘）
- `draft_manager update --set-style-id <UUID>`（风格绑定）
- `draft_manager update --set-content-type <type> [--set-ip-id <stem>]`（稿件类型）
- `draft_manager update --stage outline_refining --payload-file <json> --inject-evidence-level minimal`（大纲阶段证据包注入，默认 minimal）
- `draft_manager update --stage script_refining --payload-file <json> --inject-evidence-level full`（逐字稿阶段证据包注入，默认 full）
- `stream_gen_workflow_helper.py start-topic --direction "<方向>"`（批量 helper）

---

> **降耗提示**：本文件约 4KB，仅在进入具体阶段时 read；日常会话只读 `MEMORY_workflow.md` 索引页即可。