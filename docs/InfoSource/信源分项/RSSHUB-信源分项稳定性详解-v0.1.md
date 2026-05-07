# RSSHub 信源分项稳定性详解（v0.1）

## 信源范围

- 快讯层：`/wallstreetcn/live`、`/jin10`、`/36kr/newsflashes`
- 深度层：`/wallstreetcn/live`、`/jin10`、`/yicai/brief`、`/jiemian/lists/4`、`/36kr/newsflashes`

## 当前角色

- **快讯与深度内容核心网关层**
- 通过 `FINANCE_RSSHUB_BASE_URL` 接入

## 稳定性结论

- 路由健康时：**高**
- 路由老化或上游改版时：**中-**

## 成功点

- 有效规避直连媒体 404、DNS、反爬差异。
- 可统一 headers/timeout，降低多站点适配成本。
- 与回调修复链路（`repair-rsshub`）已经打通。

## 常见失败

- `NEWS_RSSHUB_BASE_URL_MISSING`
- `NEWS_RSSHUB_ROUTES_FAILED`
- `NEWS_RSSHUB_FILTER_EMPTY`
- `NEWS_RSSHUB_REPAIR_SUGGESTED`

## 当前兜底

- 快讯路由失败不影响行情、政策、社媒模块输出。
- 可触发人工授权更新脚本修复路由。
- 部分内容可回退直连源（deep_news 的 base sources）。

## 关键建议

1. 生产环境固定 `FINANCE_RSSHUB_BASE_URL` 并配进程守护。
2. 增加定时健康检查（路由可达率、解析成功率）。
3. 路由失败告警与 `errors` 保持一一对应，避免“假成功”。
