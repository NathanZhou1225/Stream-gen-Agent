# 金十信源分项稳定性详解（v0.1）

## 信源范围

- 直连 RSS：`https://rss.jin10.com/`
- RSSHub 路由：`/jin10`

## 当前角色

- 快讯层核心源（news_rss）
- 深度层补充源（deep_news）

## 稳定性结论

- 直连 RSS：**低到中**（受 DNS/出网影响）
- RSSHub 路由：**中上**（当前主用）

## 常见失败

- `JIN10_RSS_FAILED`
- news 路由不可达会体现为 `NEWS_RSSHUB_ROUTES_FAILED` 子集

## 当前兜底

- 优先通过 RSSHub 获取，绕过直连波动。
- 单路由失败不阻断其他新闻路由。

## 关键建议

1. 快讯层继续把 jin10 放在 RSSHub 主路由集合中。
2. 出网环境波动时优先排查 DNS，而非立即改代码。
3. 保留关键词过滤，减少非目标快讯噪声。
