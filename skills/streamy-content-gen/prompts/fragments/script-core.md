# Script Core（默认读取）

用于 `script_refining` 阶段。目标是把已确认的 `outline.json` 展开成可直接口播、可交付剪辑的 `script.json`，再由 `draft_manager.py` 自动渲染 `script.md`。

## 输入与边界

- 严格基于已落盘的 `outline.json`：`hook` / `points[]` / `cta` / `total_duration_sec` 是主骨架。
- **`meta.content_type` 已绑定时（硬约束）**：先 `draft_manager schema --stage script_refining --draft <DID> --inject-prompt-template --json`；`segments[].role` **必须**与返回的 `content_type_profile.required_segment_roles` 一致（如 `market_view` = `hook, argument_1, turn, argument_2, result, cta` 共 **6** 段）。**禁止**用通用 `hook/argument_1/turn/action/cta` 五段代替。段职责见 **`memory/rules/MEMORY_script_templates.md`**（对齐 `configs/content_templates/<type>.json`）。
- payload 路径：**`drafts/.../<DID>/_scratch/script_payload.json`**；**禁止** `write` 到 `/tmp/script_<DID>.json`。
- 大纲没写的事实不要擅自加；可增强表达，但不要改变用户已确认的结构。
- 已绑定 `style_id` 时，`draft_manager.py update` 会自动注入 `user_style_context`；若最终 payload 有该字段，必须补 `production_style_adaptation` 三项。
- 不直接写 `drafts/**`，只通过 `draft_manager.py update --stage script_refining --payload-file <json>` 写入。

## 时间预算

| 档 | Hook | 论据/场景 | 转折/冲突 | 行动/结果 | CTA | 缓冲 |
|---|---|---|---|---|---|---|
| 60s | 3-5s | 共 30-35s | 8-10s | 8-10s | 5-7s | ±2s |
| 75s | 3-5s | 共 40-45s | 10-12s | 10-12s | 6-8s | ±2s |
| 90s | 3-5s | 共 50-55s | 12-15s | 12-15s | 6-8s | ±3s |

硬约束：

- 单段不超过 20 秒。
- 总时长与 `outline.total_duration_sec` 偏差不超过 5 秒。
- Hook 必须在前 5 秒完成。

## 口语化规则

- 短句为主，15 字以内最好；一段读一遍，10 秒读不完就砍。
- 用“但是 / 所以 / 那么”，少用“然而 / 因此 / 故而”。
- 数据要口语化，例如把“涨跌幅中位数为 2.1%”改成“10 次里有一半都涨了，中间那次涨了 2%”。
- 可少量使用口头禅，但不要密集。

## 视觉与 CTA

- 每段 `visual[]` 至少 1 项，优先用 `贴纸:`、`配图:`、`特效:`、`动作:` 四类短标注。
- 60 秒稿建议 6-8 个视觉标注，均匀分布。
- CTA 只能给方法论资料或互动问题，不给股票池、荐股名单、确定性收益。
- 常用 CTA：`add_wechat`、`comment_reply`、`follow_series`。

## 合规二次扫描

生成后先自查，再交给 `draft_manager.py update` 内嵌扫描：

- 禁收益承诺：必涨、稳赚、保证收益。
- 禁绝对化：必然、绝对、唯一。
- 禁荐股暗示：懂的都懂、自己搜、心里有数加代码。
- 禁操作指令：今天买、现在卖、抄底。
- 禁贬低监管和煽动性话术。
- 数据必须带时间、口径或来源。

## 事实/观点标注

分析段必须使用真实 role，不得为了省 token 统一写 `role: "host"`。

- `argument_*` / `argument` / `turn` / `scene` / `conflict` / `result` / `action` 必须补 `claim_kind`。
- `claim_kind` 为 `fact` 或 `mixed` 时，必须补 `evidence_source_type` 与 `evidence_source_ref`。
- `hook` 与 `cta` 可省略该组字段。

## 制作附录

`production_appendix` 必须包含且只需要四块：

- `camera_shots`
- `stickers_effects`
- `visual_assets`
- `host_actions`

每块 3-5 条，可执行短句，不写空泛建议。

## 提交前自查

- `duration_sec` 是否等于各段时长加总，且贴近大纲总时长？
- Hook 是否在前 5 秒完成？
- 每段是否小于等于 20 秒？
- 每段是否有 `visual[]`？
- 分析段是否补齐事实/观点与证据字段？
- `production_appendix` 四块是否齐全且每块 3-5 条？
- 有 `user_style_context` 时是否补齐 `production_style_adaptation` 三字段？
- payload 是否没有 `display_markdown`、`stage`、`style_id`、`compliance` 等多余字段？
