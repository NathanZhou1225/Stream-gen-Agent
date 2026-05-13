# Prompt: 逐字稿生成（script-generation · slim index）

> Agent 在 `script_refining` 阶段读本索引。默认只读取 `script-core.md` 与 `script-min-schema.md`；长 few-shot 与飞书展示规则按需读取。

## 默认必读

1. `prompts/fragments/script-core.md`
   - 角色、输入边界、时长、口语化、合规、制作附录、自查清单。
   - 保留质量门禁：分析段必须使用真实 role，不得统一写 `role: "host"` 绕过事实/观点校验。

2. `prompts/fragments/script-min-schema.md`
   - `draft_manager.py schema --stage script_refining --json` 用法。
   - `update --validate-only` 写盘前校验。
   - 最小 payload 形状、字段白名单、多余字段禁传说明。

## 按需读取

- `prompts/fragments/script-examples.md`
  - 当输出连续不稳、用户要求参考样例，或需要校准不同结构时读取。
- `prompts/fragments/script-feishu-display.md`
  - 当需要把 `script.md` 贴回飞书/聊天给用户看时读取。

## 标准执行顺序

1. 读取当前 Draft 的 `outline.json` 与必要用户补充要求。
2. 读取 `script-core.md` 与 `script-min-schema.md`。
3. 构造最小 payload，不主动传 `stage`、`style_id`、`compliance`、`display_markdown`，也不传多余的 `production_appendix.*`。
4. 先执行：

```bash
python3 skills/streamy-content-gen/scripts/draft_manager.py update \
  --draft <DID> \
  --stage script_refining \
  --payload-file <payload.json> \
  --validate-only \
  --json
```

5. 若返回 `errors[]`，一次性修复全部问题。
6. 校验通过后再正式执行不带 `--validate-only` 的 `update`。
7. 读取工具返回的 `result.compliance`，向用户展示逐字稿、制作附录、合规状态与“修改还是定稿？”。

## 反馈与定稿

| 用户说 | 动作 |
|---|---|
| “Hook 太长” | 压缩 `segments[0].say`，同步 `time` |
| “论据 2 换个例子” | 改对应 segment 的 `say` / `visual` / 证据字段 |
| “口语化不够” | 重写 `say`，保持 schema 与证据字段 |
| “CTA 换成评论区” | 改 `cta` 与最后一段 `say` |
| “定稿” | `draft_manager.py finalize --draft <DID> --min-context-reset`；如用户同意 auto-refine，再加 `--auto-refine` |

## 绝对禁止

- 禁止直接写 `drafts/**/script.md` 或 `script.json`。
- 禁止在 `script_refining` 阶段重新贴 `topic_picking` 的完整行情/信源块，除非用户明确要求回看来源。
- 禁止用 `display_markdown` 让 Agent 手写展示稿；`script.md` 由工具渲染。
