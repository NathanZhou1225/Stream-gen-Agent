# 新浪信源分项稳定性详解（v0.1）

## 信源范围

- 行情：`hq.sinajs.cn`（A 股三大指数、港股指数、可选海外/大宗）
- 资讯：新浪财经 7x24（`zhibo.sina.com.cn` feed API）

## 当前角色

- **主干层**：A 股三大指数、港股指数
- **增强层**：7x24 作为全球宏观和北向文本降级来源

## 稳定性结论

- A 股/港股指数：**高**
- 7x24：**中上**
- 海外/大宗（stub）：**中**（可选开启）

## 成功点

- 不依赖 token，云端部署成本低。
- 相比东财链路更抗 WAF，适合作为默认主路径。
- 7x24 与北向文本抽取、宏观热点拼接契合度高。

## 常见失败

- `SINA_TRINITY_FAILED` / `SINA_TRINITY_EMPTY`
- `SINA_HK_INDICES_FAILED`
- `SINA_LIVE_FEED_FAILED` / `SINA_LIVE_EMPTY` / `SINA_LIVE_SHAPE`
- `SINA_HQ_FAILED`（海外/大宗 stub）

## 当前兜底

- 指数失败时保留占位结构，避免下游解析中断。
- 北向可降级到 Tushare 与 RSSHub 文本抽取。
- 7x24 失败不阻断整体快照，仅在 `errors` 报告。

## 关键建议

1. 继续保持新浪作为指数主路径。
2. 保留 7x24 关键词过滤词表，按业务主题定期微调。
3. 非交易时段不把 7x24 空结果误判为链路故障。
