# Prompt: 大纲生成（outline-generation · slim index）

> Agent 在 `outline_refining` 阶段读本索引。默认读取 `outline-core.md` 与 `outline-min-schema.md`；完整 few-shot 按需读取。

## 默认必读

1. `prompts/fragments/outline-core.md`  
   角色、输入、五种结构、段职责与红线、合规、`production_hint`、`display_markdown` 展示原则、用户反馈表、自查清单。

2. `prompts/fragments/outline-min-schema.md`  
   `draft_manager.py schema --stage outline_refining`、`update --validate-only`、最小 payload 形状、字段白名单、禁传字段。

## 按需读取

- `prompts/fragments/outline-examples.md`  
  当输出结构不稳、用户要样例、或需对齐标准型/反转型完整 JSON 时读取。

## 标准执行顺序

1. 读 `topic_candidates.json`（`chosen`、证据锚点、用户追加约束）；若已绑 `style_id`，注意工具可能已注入 `user_style_context`（以 `validate-only` 结果为准）。
2. 读 `outline-core.md` + `outline-min-schema.md`。
3. 组 payload；先执行：

```bash
python3 skills/streamy-content-gen/scripts/draft_manager.py update \
  --draft <DID> \
  --stage outline_refining \
  --payload-file <payload.json> \
  --validate-only \
  --json
```

4. 若 `errors[]` 非空，一次性修复后重试。
5. 通过后去掉 `--validate-only` 正式落盘，并按 `outline-core` 在对话中展示大纲（阶段边界：不重复贴选题阶段市场块）。

## 与流水线的关系

- 须在 **方向证据包** 已落盘、`style_id` 已绑定之后，才应进入本阶段（见 `SKILL.md` 与 `draft_manager` 门禁）。
- 用户要「换方向」回退选题：见 `natural-language-intent.md` 索引 → `intent-core.md`（`rewind_to_topic` / `update --stage topic_picking`），不要在大纲 JSON 上硬改主题糊弄过关。
