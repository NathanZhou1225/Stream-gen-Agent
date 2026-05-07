# 微博热搜（vvhan）信源分项稳定性详解（v0.1）

## 信源范围

- API：`https://api.vvhan.com/api/hotlist/wbHot`
- 用途：社媒梯队第一层

## 当前角色

- **社媒热点首选源**（若可用）
- 失败后自动降级到 AkShare/百度

## 稳定性结论

- 当前环境：**低**
- 典型问题：DNS/出网异常

## 成功点

- 接口简洁，命中时可直接得到热门词和热度。
- 与财经关键词过滤结合后，能快速得到“可用社媒话题”。

## 常见失败

- `SOCIAL_WB_HOT_FAILED`
- `SOCIAL_WB_HOT_PARSE`
- `SOCIAL_WB_HOT_EMPTY`
- `SOCIAL_WB_HOT_NO_MACRO_MATCH`

## 当前兜底

- 自动回退到 AkShare 社媒热榜（二级）
- 二级仍失败则回退百度热榜（三级）
- 失败不阻断整体快照

## 关键建议

1. 将 vvhan 视为“可用即增益”的可选源，不做强依赖。
2. 为 DNS/出网异常加运维探针（避免误判成 API 下线）。
3. 若要提升稳定性，优先引入可签约的社媒 JSON API。
