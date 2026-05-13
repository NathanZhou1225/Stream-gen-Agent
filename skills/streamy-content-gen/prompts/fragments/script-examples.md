# Script Examples（按需读取）

只有在模型连续输出不合格、用户要求参考样例，或需要校准风格时读取本文件。

## 示例 A：标准结构 60s

```json
{
  "draft_id": "<DID>",
  "title": "降准不等于立刻涨",
  "duration_sec": 58,
  "structure_template": "standard",
  "segments": [
    {
      "time": "0:00-0:04",
      "role": "hook",
      "say": "过去 10 次降准，A 股平均 30 天涨 2.1%——但这个数据，骗了很多人。",
      "visual": ["贴纸:惊叹", "特效:zoom-in"],
      "cta_hint": "这张复盘表我等会儿发"
    },
    {
      "time": "0:04-0:16",
      "role": "argument_1",
      "say": "我拉了 2008 到 2023 这 10 次降准，后面 30 天上证的涨跌幅。中位数是涨 2.1%，但有 3 次是跌的，最深跌了 6%。",
      "claim_kind": "fact",
      "evidence_source_type": "market",
      "evidence_source_ref": "tushare:index_daily",
      "visual": ["配图:柱状图:历次降准 30 日涨跌"],
      "cta_hint": null
    },
    {
      "time": "0:16-0:29",
      "role": "argument_2",
      "say": "涨的那几次和跌的那几次，有个共同点：宏观背景完全不一样。2015 年央行放水的时候，汇率稳；2022 年那次，人民币一直在贬。",
      "claim_kind": "mixed",
      "evidence_source_type": "announcement",
      "evidence_source_ref": "央行公告/宏观口径对比",
      "visual": ["配图:对比图:2015 vs 2022 宏观"],
      "cta_hint": null
    },
    {
      "time": "0:29-0:39",
      "role": "turn",
      "say": "那这次呢？我看有两点不一样：第一，美联储还在加息末端；第二，人民币汇率离 7.3 就差两步。",
      "claim_kind": "mixed",
      "evidence_source_type": "market",
      "evidence_source_ref": "美联储利率区间/人民币汇率位置",
      "visual": ["贴纸:数字 1", "贴纸:数字 2"],
      "cta_hint": null
    },
    {
      "time": "0:39-0:51",
      "role": "action",
      "say": "所以别急着抄作业。盯住三个东西：DR007 看钱贵不贵，北上资金看外资怎么看，中美利差看人民币压力。",
      "claim_kind": "opinion",
      "visual": ["配图:指标三件套图"],
      "cta_hint": null
    },
    {
      "time": "0:51-0:58",
      "role": "cta",
      "say": "我把这 10 次降准的完整复盘做成一张表了。想要的，评论区扣 1。",
      "visual": ["贴纸:箭头", "动作:手指屏幕"],
      "cta_hint": null
    }
  ],
  "cta": {"type": "comment_reply", "position": "ending", "phrasing": "评论区扣 1"},
  "production_appendix": {
    "camera_shots": ["Hook 用近景推入", "论据段切中景并让出图表位", "转折段轻推镜", "CTA 回到近景"],
    "stickers_effects": ["Hook 叠惊叹贴纸", "论据1用箭头标高低点", "转折段上黄底字幕", "CTA 加评论区箭头"],
    "visual_assets": ["历次降准涨跌柱状图", "宏观背景对比图", "三指标观察卡"],
    "host_actions": ["Hook 抬眉停顿", "讲指标时手势计数", "转折前微摇头", "CTA 指向评论区"]
  },
  "source": {"topic": "央行降准+A股历史表现", "data_sources": ["tushare:index_daily"]}
}
```

## 示例 B：反转结构 90s

```json
{
  "draft_id": "<DID>",
  "title": "AI 算力不是泡沫：它是脚手架",
  "duration_sec": 87,
  "structure_template": "reversal",
  "segments": [
    {"time": "0:00-0:05", "role": "hook", "say": "说 AI 算力是泡沫的人，可能搞错了两件事。", "visual": ["贴纸:疑问", "特效:zoom-in"], "cta_hint": "想听完整逻辑的评论区扣脚手架"},
    {"time": "0:05-0:18", "role": "scene", "say": "现在主流担心算力泡沫，理由两个：估值高，应用还没爆发，硬件先涨了。", "claim_kind": "mixed", "evidence_source_type": "market", "evidence_source_ref": "市场估值/应用商业化进度", "visual": ["配图:硬件估值卡"], "cta_hint": null},
    {"time": "0:18-0:38", "role": "conflict", "say": "但这个对比可能错了。算力不是终端产品，它更像基础设施，像铁路，也像光纤。", "claim_kind": "opinion", "visual": ["配图:铁路+光纤+算力拼接"], "cta_hint": null},
    {"time": "0:38-0:58", "role": "result", "say": "我的判断是，下一轮泡沫不一定在算力层，更可能在应用层。硬件已经跑在前面，但应用付费率才刚起步。", "claim_kind": "opinion", "visual": ["配图:硬件 vs 应用层对比表"], "cta_hint": "等会儿发对比表"},
    {"time": "0:58-1:15", "role": "action", "say": "接下来就看三个信号：应用付费转化率、算力利用率、巨头资本开支拐点。三个联动变，风向才是真的变。", "claim_kind": "opinion", "visual": ["贴纸:数字 1/2/3"], "cta_hint": null},
    {"time": "1:15-1:27", "role": "cta", "say": "想要这张硬件和应用层的对比表，评论区扣 3。", "visual": ["贴纸:箭头", "动作:手指屏幕"], "cta_hint": null}
  ],
  "cta": {"type": "comment_reply", "position": "triple", "phrasing": "评论区扣 3"},
  "production_appendix": {
    "camera_shots": ["Hook近景推入", "场景段中景留图表位", "冲突段快切素材", "CTA近景收束"],
    "stickers_effects": ["Hook疑问贴纸", "估值词高亮", "三信号逐条弹出", "评论区箭头"],
    "visual_assets": ["硬件估值卡", "铁路光纤算力拼图", "硬件应用层对比表"],
    "host_actions": ["Hook停顿半秒", "讲两个理由时手势计数", "讲三信号时连续比数", "结尾指评论区"]
  },
  "source": {"topic": "AI 算力是不是泡沫", "data_sources": []}
}
```
