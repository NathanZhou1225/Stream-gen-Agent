---
name: streamy-content-gen
description: |
  Streamy 短视频/口播：Draft 多阶段（topic_picking → outline_refining → script_refining）与合规闸；支持 finance-source-ingest 事实管道。
  带明确方向开稿：须先 preflight_topic.py；先进入 topic_picking 展示候选，用户选定候选后必须生成该方向 evidence_pack，再进入 user-style 与大纲；候选每条必须含标题+核心论点+3条论据；draft_id 须为 draft_manager 三字符 + active/default 目录；禁止同轮越级输出大纲/逐字稿/分镜成稿。
---

# streamy-content-gen（核心契约 · 精简版）

> **降耗提示**：本文件约 4KB（核心契约）；CLI 参考约 6KB 已移至 `docs/cli_reference.md`，按需 read。预计每轮减少 **2000-3000 Token**。

---

## 三段式门禁（P0 硬约束）

**失效模式**：事实拉取成功 → 模型直接在聊天里写大纲/逐字稿 → 绕过落盘与审计。**禁止**。

1. **阶段名**：`topic_picking` → `outline_refining` → `script_refining` → `finalized`。不得自创、不得跳步。

2. **越级成稿禁令**：
   - 用户请求「带方向开稿 / 要选题 / 要大纲 / 要逐字稿」→ 未落盘前**禁止**输出大纲/逐字稿正文
   - **唯一例外**：用户明确只要「闲聊讲解、不要 draft」

3. **单次回复边界**：
   - preflight 后 → `draft_manager update --stage topic_picking` → 展示三候选 + 选号 → **不得同轮写大纲/逐字稿**
   - 用户选定 → `--apply-topic-choice <N>` → 展示 evidence_pack → **一次确认**稿件类型+风格（组合选项或 `bind-profile`）→ outline

4. **何时写大纲/逐字稿**：
   - 大纲：`topic_picking` 完成 + evidence_pack 落盘 + 风格绑定 → `--stage outline_refining`
   - 逐字稿：`outline_refining` 落盘 → `--stage script_refining`

---

## 带方向开稿契约（P0）

用户提供**明确方向**并要求开稿 → **必须先** preflight：

```
python3 scripts/preflight_topic.py --direction '用户自然语言方向'
```

- stdout 为单一 JSON：`ok: true` + `topic_payload`（含 `candidate_evidence_packs` 预计算）
- **缓存优先**：读 `cache/snapshot/snapshot.json`（`snapshot_cache.py`：优先比对云端 `db_last_ingested_at`，失败回退 6h 墙钟）；冷拉由 `query_market_facts --full` 或 `scripts/warm_snapshot_cache.sh` 写入
- **预热 cron**：`scripts/setup_snapshot_warm_cron.sh --install`（08:15 / 09:45 / 14:05 / 20:05 CST）；**每台业务机各自 warm**；**迁移/换路径须在新根重装**（见 `docs/DEPLOY_WORKBUDDY.md` §3.1）
- **定向信息**：`query_direction_brief.py --direction '…'`（读 cache，不贴全量表）
- **关热榜**：默认 `PREFLIGHT_SKIP_HOT_RANK=1`（或 `preflight_topic.py --no-hot-rank`）
- **禁止**默认 `--allow-targeted-fetch`（证据不足见 `source_gaps`，运维可显式开启）
- 用户选定后 `--apply-topic-choice <N>`（一次写入 chosen + evidence_pack，无需二次 preflight）
- legacy 回退：仅当 `topic_candidates` 无 `candidate_evidence_packs` 时用 `--candidate-id` + 分步落盘

**禁止同轮续写大纲**：证据包 + 稿件类型 + 风格门禁完成后才进 outline。

---

## 纯拉数契约

用户「今日行情 / 热点 / 全量 / 信源快照」任一口径 + **未**进入开稿链：

```
python3 scripts/query_market_facts.py --sources market,news,social --summary-only
```

- 返回 JSON 的 `markdown_summary` **全文原样**展示
- 禁止拆成只行情/只热点、禁止删段
- 禁止用三条候选标题复述用户原话却不展示事实锚点

---

## 飞书「去工程化」输出

- **禁止**：直播内部步骤（读哪个文件、拉 schema、校验前后状态）、复述 CLI 路径、贴英文报错
- **允许**：单条消息交付里程碑产物（证据包、大纲、逐字稿）
- **稿件类型**：对用户用中文（大盘观点 / 投教 / 人设介绍）；slug 仅在工具参数

---

## Draft ID 契约（P0）

- 正式稿件 `draft_id` = draft_manager 分配的三位 Base36（如 `PBG`）
- 目录：`drafts/active/default/<draft_id>/`
- 禁止：日期 + 中文文件夹名、扁平 `*-outline.md`、用户可见长串替代三字符 ID

---

## Draft 结构诊断（P0）

- 禁止直接写 `outline.md` / `script.md` / `meta.json` 推阶段
- 大纲/逐字稿必须通过 `draft_manager update --payload-file <json>` 落盘
- 怀疑历史稿绕过工具 → `doctor --draft <DID> --json`
- 进入 `script_refining` 前检查大纲是否有 `production_hint`
- 定稿前检查逐字稿是否有 `production_appendix` 四块

---

## user-style-manager 硬门禁（P0）

- 进 `outline_refining` 前必须：
  - `list --with-context` 展示所有风格
  - 用户明确选择
  - `--set-style-id <UUID>` 绑定
- 不得用"默认风格/通用风格"跳过
- `topic_picking` 不强制带风格；风格块主要在大纲/逐字稿阶段约束语气

---

## 证据包注入级别控制（v0.3 新增）

`draft_manager update` 新增 `--inject-evidence-level minimal|full` 参数：

- **minimal**（大纲阶段默认）：只注入 `core_facts[]` + `candidate_title`（约 200-300 Token）
- **full**（逐字稿阶段按需）：注入完整 evidence_pack（含 `detailed_sources`、`argument_boosters`）

**降耗提示**：大纲阶段使用 minimal 可减少 **500-800 Token**。

---

## 迁移性

- 仅依赖：`preflight_topic.py` + `finance-source-ingest`（兄弟目录）+ `python3`
- 不侵入 ingest CLI 契约（仅 subprocess）
- 兄弟技能根目录可通过 `--finance-root` 覆盖

---

## CLI 快速参考（精简）

| 常用命令 | 说明 |
|---------|------|
| `preflight_topic.py --direction '<方向>'` | 拉快照 + 生成三候选 + 预计算证据包 |
| `draft_manager update --apply-topic-choice <N>` | 证据包落盘（一次写入） |
| `draft_manager update --set-style-id <UUID>` | 风格绑定 |
| `draft_manager update --set-content-type <type>` | 稿件类型 |
| `stream_gen_workflow_helper.py list-profile-options` | 类型×风格组合（飞书一次选型） |
| `stream_gen_workflow_helper.py bind-profile --draft <DID> --content-type <type> --style-id <UUID>` | 类型+风格一步绑定 |
| `query_direction_brief.py --direction '<方向>'` | 定向信息简报（读 cache，非全量表） |
| `draft_manager update --stage outline_refining --payload-file <json> --inject-evidence-level minimal --validate-only` | 大纲预检（minimal 证据包，降耗） |
| `draft_manager update --stage script_refining --payload-file <json> --inject-evidence-level full --validate-only` | 逐字稿预检（full 证据包） |
| `draft_manager schema --stage outline_refining --inject-prompt-template --json` | 拉 schema + prompt 模板（省单独 read，约 -1000~1500 Token） |
| `draft_manager schema --stage script_refining --inject-prompt-template --json` | 同上，逐字稿阶段 |
| `draft_manager finalize --draft <DID> --auto-refine --min-context-reset` | 定稿归档 |
| `stream_gen_workflow_helper.py start-topic --direction '<方向>'` | 批量 helper（创建 + preflight + topic_picking） |

> **完整 CLI 参考**（含参数、路径、错误码）见 `docs/cli_reference.md`。

---

## 相关碎片

- `memory/rules/workflow_gate.md`：详细门禁规则（约 2.9KB）
- `memory/rules/MEMORY_script_templates.md`：逐字稿模板
- `prompts/outline-core.md`、`prompts/script-core.md`：prompt 碎片
- `docs/cli_reference.md`：完整 CLI 参考（约 6KB）

---

> **精简后本文件约 4KB**；原版约 10KB。预计每轮减少 **2000-3000 Token** 规则读取。