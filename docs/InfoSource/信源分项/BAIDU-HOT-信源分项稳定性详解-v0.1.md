# 百度热榜信源分项稳定性详解（v0.1）

## 信源范围

- 主接口：`https://top.baidu.com/api/board?platform=wise&tab=realtime`
- 社媒三级兜底：`https://top.baidu.com/board?tab=realtime`（HTML）

## 当前角色

- **宏观热点补位源**
- **社媒三级降级源**

## 稳定性结论

- 整体：**中**
- 风险：接口字段变化与财经关键词命中不足

## 成功点

- 无需 key，常可被服务端访问。
- 在微博与 AkShare 热榜失败时仍可兜底提供热点主题。

## 常见失败

- `MACRO_HOT_FETCH_FAILED`
- `MACRO_HOT_JSON`
- `MACRO_HOT_FINANCE_FILTER_EMPTY`
- `BAIDU_HOT_FAILED`
- `BAIDU_HOT_NO_MATCH`

## 当前兜底

- 先尝试结构化 JSON；失败时可回退 HTML 提取。
- 财经关键词过滤无命中时，明确告警而非“伪热点”输出。

## 关键建议

1. 维持 JSON+HTML 双通道，降低单路由失效风险。
2. 持续调优财经过滤词，降低“泛娱乐词”噪声。
3. 在用户只看金融快照时保持保守输出，不强行填充非财经热词。
