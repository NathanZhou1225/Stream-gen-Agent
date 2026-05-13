# Script Minimal Schema（默认读取）

本文件只放逐字稿最小通过契约。完整示例与飞书展示规则不默认读取。

## 单一真相源

生成 payload 前先查看工具模板：

```bash
python3 skills/streamy-content-gen/scripts/draft_manager.py schema --stage script_refining --json
```

写入前先做 dry-run 校验：

```bash
python3 skills/streamy-content-gen/scripts/draft_manager.py update \
  --draft <DID> \
  --stage script_refining \
  --payload-file <payload.json> \
  --validate-only \
  --json
```

若返回 `errors[]`，一次性修完全部错误，再正式执行不带 `--validate-only` 的 `update`。

## 最小 payload 形状

```json
{
  "draft_id": "<DID>",
  "title": "标题",
  "duration_sec": 60,
  "structure_template": "standard",
  "segments": [
    {
      "time": "0:00-0:05",
      "role": "hook",
      "say": "开场钩子，直接说明为什么值得听。",
      "visual": ["贴纸:重点"],
      "cta_hint": null
    },
    {
      "time": "0:05-0:22",
      "role": "argument_1",
      "say": "第一条事实论据，带时间范围、数据口径或来源。",
      "claim_kind": "fact",
      "evidence_source_type": "market",
      "evidence_source_ref": "来源/指标/时间范围",
      "visual": ["配图:关键数据卡"],
      "cta_hint": null
    },
    {
      "time": "0:22-0:42",
      "role": "turn",
      "say": "转折或解释：为什么这条信息不能只看表面。",
      "claim_kind": "mixed",
      "evidence_source_type": "news_flash",
      "evidence_source_ref": "来源/发布时间/标题",
      "visual": ["特效:转场"],
      "cta_hint": null
    },
    {
      "time": "0:42-0:55",
      "role": "action",
      "say": "给观众一个观察方法，不给具体买卖建议。",
      "claim_kind": "opinion",
      "visual": ["配图:观察指标清单"],
      "cta_hint": null
    },
    {
      "time": "0:55-1:00",
      "role": "cta",
      "say": "想看完整清单，评论区回复关键词。",
      "visual": ["动作:手指评论区"],
      "cta_hint": null
    }
  ],
  "cta": {
    "type": "comment_reply",
    "position": "ending",
    "phrasing": "想看完整清单，评论区回复关键词"
  },
  "production_appendix": {
    "camera_shots": ["Hook近景推入", "论据段中景留右侧图表位", "转折段轻推镜"],
    "stickers_effects": ["Hook加重点贴纸", "数据出现时加箭头", "CTA加评论区箭头"],
    "visual_assets": ["关键数据卡", "来源截图或摘要卡", "观察指标三联表"],
    "host_actions": ["Hook停顿半秒", "讲三点时手势计数", "CTA指向评论区"]
  },
  "source": {
    "topic": "主题",
    "data_sources": ["来源/指标/时间范围"]
  }
}
```

若 `validate-only` 结果显示工具自动注入了 `user_style_context`，必须补：

```json
{
  "production_style_adaptation": {
    "ip_style_adaptation": "说明本稿结构如何贴合该 IP",
    "tone_style_adaptation": "说明口语节奏和句式如何贴合该风格",
    "visual_style_adaptation": "说明镜头/贴纸/配图如何贴合该风格"
  }
}
```

## 字段白名单

顶层建议字段：

- 必填：`draft_id`、`title`、`duration_sec`、`segments`、`production_appendix`
- 推荐：`structure_template`、`cta`、`source`
- 条件必填：`production_style_adaptation`（当 `user_style_context` 非空时）

`segments[]` 字段：

- 必填：`time`、`role`、`say`
- 推荐：`visual`、`cta_hint`
- 分析段必填：`claim_kind`
- `fact` / `mixed` 分析段必填：`evidence_source_type`、`evidence_source_ref`

`production_appendix` 只保留四块：

- `camera_shots`
- `stickers_effects`
- `visual_assets`
- `host_actions`

## 禁止主动传入的多余字段

这些字段会浪费 token，或由工具/meta 自动负责：

- 顶层：`stage`、`style_id`、`compliance`、`display_markdown`
- `production_appendix` 内：`camera`、`background`、`lighting`、`subtitle`、`bgm`、`post`、`references`

## 不采纳的“省 token 技巧”

- 不要把分析段统一写成 `role: "host"`。这会绕开事实/观点证据校验，破坏金融内容审计链。
- 不要刻意不传 `user_style_context`。已绑定 `style_id` 时，工具会自动注入；风格适配是质量门禁，不是可省略项。
