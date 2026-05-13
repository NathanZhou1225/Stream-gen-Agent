# Outline Few-shot（按需读取）

当结构连续不稳、用户明确要求参考示例、或需对齐某种 `structure_template` 时，再读本节。日常默认只读 `outline-core.md` + `outline-min-schema.md`。

---

## 示例 A：标准型 · 数据派

**方向**：`chosen=2`，「过去 10 次降准，30 日中位数 +2.1%」

```json
{
  "title": "降准不等于立刻涨：10 次样本告诉你的真相",
  "structure_template": "standard",
  "hook": {"text": "过去 10 次降准，A 股平均 30 天涨 2.1%——但这个数据骗了很多人", "duration_sec": 3},
  "points": [
    {"order": 1, "role": "argument", "headline": "10 次降准的 30 日涨跌分布",
     "evidence": "tushare index_daily 2008-2023 降准后 30 个交易日涨跌幅", "production_hint": "中景口播+柱状图逐列高亮", "duration_sec": 12},
    {"order": 2, "role": "argument", "headline": "涨跌分化的根源：宏观背景",
     "evidence": "对比 2015 vs 2022 两次降准时的 CPI / 汇率 / 外部流动性", "production_hint": "左右分屏做年份对比", "duration_sec": 13},
    {"order": 3, "role": "turn", "headline": "这次和过去有两点关键不同",
     "evidence": "美联储加息周期 + 人民币汇率位置", "production_hint": "近景停顿后上关键不同字幕", "duration_sec": 10},
    {"order": 4, "role": "action", "headline": "3 个后续跟踪指标",
     "evidence": "DR007 / 北上资金 / 中美利差", "production_hint": "三分屏列指标并加箭头动效", "duration_sec": 12}
  ],
  "cta": {"type": "add_wechat", "headline": "领取《历次降准复盘表》"},
  "total_duration_sec": 57,
  "compliance_preview": {
    "checked_rules": ["no_specific_stock", "data_with_timeframe", "action_stops_at_method"],
    "warnings": []
  },
  "display_markdown": "──── 大纲 #<DID> ────\n[Hook · 3s]  过去 10 次降准，A 股平均 30 天涨 2.1%——但这个数据骗了很多人\n[论据 1]     10 次降准的 30 日涨跌分布\n[论据 2]     涨跌分化的根源：宏观背景\n[转折]       这次和过去有两点关键不同\n[行动启发]   3 个后续跟踪指标\n[CTA]        领取《历次降准复盘表》\n────\n合计时长：约 57 秒\n"
}
```

---

## 示例 B：反转型 · 反直觉钩子

**方向**：`chosen=1`，「AI 算力不是泡沫，它是下一轮泡沫的脚手架」

```json
{
  "title": "AI 算力不是泡沫：它是下一轮泡沫的脚手架",
  "structure_template": "reversal",
  "hook": {"text": "说 AI 算力是泡沫的人，可能搞错了两件事", "duration_sec": 4},
  "points": [
    {"order": 1, "role": "scene", "headline": "大家以为的算力泡沫",
     "evidence": "市场主流担忧：高 PE / 需求持续性", "production_hint": "先黑底白字抛常见质疑", "duration_sec": 10},
    {"order": 2, "role": "conflict", "headline": "真相：算力是基础设施不是终端产品",
     "evidence": "类比 1870s 铁路 / 2000 互联网基建", "production_hint": "历史素材快切+时间轴叠加", "duration_sec": 15},
    {"order": 3, "role": "result", "headline": "下一轮泡沫在应用层不在算力层",
     "evidence": "应用层 vs 硬件层估值与现金流对比", "production_hint": "双列对比卡片+红绿标识", "duration_sec": 15},
    {"order": 4, "role": "action", "headline": "该关注什么：三个信号",
     "evidence": "应用付费率 / 算力利用率 / 头部资本开支", "production_hint": "三条清单逐条弹出收尾", "duration_sec": 10}
  ],
  "cta": {"type": "comment_reply", "headline": "评论区扣 '3' 我发对比表"},
  "total_duration_sec": 57,
  "compliance_preview": {"checked_rules": ["no_specific_stock", "data_with_timeframe"], "warnings": []},
  "display_markdown": "（按 outline-core §6 格式自行填满，与 segments 一致）"
}
```
