---
name: user-style-manager
description: |
  管理用户口播/短视频的「风格记忆」：从历史文稿用 LLM 抽特征（非 fine-tune），存 SQLite（workspace/user_data/）；
  支持多标签、list、get-context RAG 块。与 streamy-content-gen 的 draft `style_id` 联动。提取需可用 Ark/OpenAI 兼容凭据。
---

# user-style-manager

## 数据落点

- 数据库：`{WORKSPACE_ROOT}/user_data/style_memory.db`（**不在**本 skill 目录内，便于只迁移 `skills/` 不带走用户数据）
- 用户 id：默认 `OPENCLAW_USER_ID`（未设则 `default`），CLI 可 `--user-id` 覆盖（如飞书 `open_id`）

## 命令（在 workspace 根执行，路径按你机器调整）

```bash
python3 skills/user-style-manager/scripts/style_cli.py init-db
python3 skills/user-style-manager/scripts/style_cli.py list --with-context --json
python3 skills/user-style-manager/scripts/style_cli.py extract --text-file ./sample.txt --tags "demo,口播"
python3 skills/user-style-manager/scripts/style_cli.py import --json-file ./handmade.json
python3 skills/user-style-manager/scripts/style_cli.py get-context --style-id <UUID> --format json
```

### 持续优化（同一条 `style_id`，越用越准）

- 在用户有新定稿、认可片段或修正后的口播文时，**不要**再 `extract` 成新 UUID；用 **`refine`** 将旧画像与新样本文 **合并写回同一条**（`refine_count` +1，`style_id` 不变，`draft` 上绑定的 `style_id` 无需改）。
- 需 **Ark** 一条（`prompts/refine-style.md`），与 `extract` 等价的凭据；**不**是模型微调。

```bash
python3 skills/user-style-manager/scripts/style_cli.py refine --style-id <UUID> --text-file ./new_sample.txt
# 成稿在 stdin：  cat final_script.txt | python3 .../style_cli.py refine --style-id <UUID>
```

- 产品侧可约定：定稿/用户确认后由 Agent **自动**调用 `refine`（用 `script.md` 或导出的成稿为样本）。`list` 的 JSON 中含 **`refine_count` / `updated_at`** 便于运营观察优化次数。

## 与 streamy-content-gen

- 建稿 `draft_manager create --style-id <UUID>` 或 `update --set-style-id` 将风格绑定到当前 Draft
- **飞书选型**：`list --with-context` 每条含 `context_preview`（截断 RAG 块）；或 helper `list-styles` / `bind-style`（一步绑定并返回 `user_style_context`）
- 进入 `outline_refining` / `script_refining` 的 payload 可含 **`user_style_context`**；已绑 `style_id` 且 payload 未带时，`draft_manager update` 会自动 `get-context` 注入

## 安全

- 不得把用户原文/切片复制进本 SKILL.md
- 数据库可能含 PII，备份与访问控制与 `drafts/` 同级对待

## 凭据

- 提取 / 持续优化（`refine`）：`style_extract` 读 `OPENCLAW_ARK_*` 或 `OPENCLAW_CONFIG` 指向的 `openclaw.json` 中 `models.providers.ark`（**不在** skill 内写 key）

## 本地缓存（省 API）

- `extract` / `refine` 在 **相同输入** 下会命中 `{WORKSPACE_ROOT}/user_data/style_extract_cache/*.json`，跳过重复调用 Ark。改 `prompts/extract-style.md` 或 `refine-style.md` 后如需忽略旧缓存，可删该目录下文件或设环境变量 **`STYLE_EXTRACT_CACHE=0`** 临时关闭。
