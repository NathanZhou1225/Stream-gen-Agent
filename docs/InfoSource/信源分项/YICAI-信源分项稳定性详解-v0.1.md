# 第一财经信源分项稳定性详解（v0.1）

## 信源范围

- 直连 RSS 候选：
  - `https://www.yicai.com/rss/news/`
  - `https://www.yicai.com/rss/list/`
  - `https://www.yicai.com/rss/`
- RSSHub 路由：`/yicai/brief`

## 当前角色

- 深度内容与宏观资讯补充源

## 稳定性结论

- 直连 RSS：**低**（404 高频）
- RSSHub 路由：**中上**（当前可用）

## 常见失败

- `YICAI_RSS_FAILED`（404 或路由不可达）

## 当前兜底

- 使用 RSSHub 的 yicai 路由作为主可用路径。
- 直连失败不会影响深度层整体输出。

## 关键建议

1. 继续保持“RSSHub 主、直连辅”策略。
2. 将 404 视为结构性失败，不做频繁重试浪费资源。
3. 若需提升稳定度，可引入商业新闻 API 替代直连 RSS。
