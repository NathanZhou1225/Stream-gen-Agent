# 华尔街见闻信源分项稳定性详解（v0.1）

## 信源范围

- 直连 API：
  - `https://api.wallstreetcn.com/apiv1/content/lives?...`
  - `https://api.wallstreetcn.com/apiv1/content/articles?...`
- RSSHub 路由：`/wallstreetcn/live`

## 当前角色

- 快讯层与深度层的重要国际宏观来源

## 稳定性结论

- 直连 API：**低到中**（经常空返回）
- RSSHub 路由：**中上**（当前主用）

## 常见失败

- `WALLSTREETCN_API_EMPTY`
- 直连请求超时/不可达（记录在 deep_news errors）

## 当前兜底

- 优先 RSSHub，直连仅作回退。
- 单源失败不影响其他深度源与主快照成功。

## 关键建议

1. 默认把 `wallstreetcn_rsshub` 作为主消费标识。
2. 直连 API 仅保留容灾价值，不作为可用性 KPI。
3. 对空返回单独统计，避免误判成“无新闻日”。
