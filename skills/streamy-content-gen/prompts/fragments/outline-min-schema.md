# Outline Minimal Schema（默认读取）

大纲阶段**只放逐盘契约**：与 `outline-core.md` 同轮必读。完整 few-shot 见 `outline-examples.md`。

## 单一真相源

生成前先拉工具模板（阶段名用下划线，**不要**写 `outline` / `script` 简写）：

```bash
python3 skills/streamy-content-gen/scripts/draft_manager.py schema --stage outline_refining --json
```

写盘前先 dry-run：

```bash
python3 skills/streamy-content-gen/scripts/draft_manager.py update \
  --draft <DID> \
  --stage outline_refining \
  --payload-file <payload.json> \
  --validate-only \
  --json
```

若返回 `errors[]`，**一次性**修完再正式执行不带 `--validate-only` 的 `update`。

## 最小 payload 形状（与工具模板对齐）

顶层字段与 `_outline_min_schema()` 一致，要点：

- `title`、`structure_template`、`hook`（`text` + `duration_sec`）
- `points[]`：每项含 `order`、`role`、`headline`、`evidence`、`production_hint`（**必填**，≤36 字）、`duration_sec`
- `cta`：`type` + `headline`
- `total_duration_sec`
- `compliance_preview`（推荐）
- **`display_markdown`**：字符串，供工具写 `outline.md`（见 `outline-core.md` §6）

`structure_template`：`standard` / `reversal` / `listicle` / `story` / `debate`。

`points[].role`：`argument` / `turn` / `action` / `scene` / `conflict` / `result` 等（与方向结构一致）。

## 字段白名单（建议）

**应出现**（与落盘 `outline.json` 一致）：

- `title`、`structure_template`、`hook`、`points`、`cta`、`total_duration_sec`
- 推荐：`compliance_preview`
- **条件**：已绑定 `style_id` 且工具**未**自动注入满额上下文时，可按 SKILL 补 `user_style_context`；多数情况下工具会从 `style_cli get-context` **自动注入** `user_style_context`，以 `validate-only` 返回为准。

## 禁止主动传入的多余字段

浪费 token 或与 meta/CLI 重复、易污染 JSON 的字段**不要**放进 payload，例如：

- 顶层：`draft_id`、`stage`、`style_id`（由 `meta` / 会话管理）
- 不要把整段 `topic_candidates` 再嵌一份进 outline payload
- 其它与大纲 JSON schema 无关的调试键

`display_markdown` **需要**传入（供渲染 `outline.md`），与逐字稿阶段对 `display_markdown` 的废弃策略**不同**。

## 不采纳的「省 token 技巧」

- 省略 `production_hint` 或统一写空话 → 工具报 `OUTLINE_PRODUCTION_HINT_*`。
- `points[]` 为空或过短 → `OUTLINE_POINTS_MISSING`。
- 忽略 `user_style_context`（已注入时）→ 风格与合规仍须服从 `outline-core` §2。
