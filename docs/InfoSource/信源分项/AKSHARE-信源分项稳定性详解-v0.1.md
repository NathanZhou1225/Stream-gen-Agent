# AKShare 信源分项稳定性详解（v0.1）

> **目的**：补齐《金融信源梳理与扩充》里对 AkShare 颗粒度不够的问题。  
> **范围**：`workspace-stream-gen/skills/finance-source-ingest` 当前代码链路。  
> **最后更新**：2026-04-30

---

## 1. 总览结论（先看这个）

AkShare 在当前系统里不是“单一信源”，而是按信息类型分散使用，稳定性差异很大：

- **相对可用（条件稳定）**：行业资金、北向主探测、财联社快讯、东财人气榜。
- **不稳定（云端波动明显）**：涨跌停池、北向在交易时段口径波动、部分资金探测字段。
- **当前基本失败（函数缺失或版本不兼容）**：`stock_hot_tgb`、`stock_hot_rank_wc`。
- **已降级为增强角色**：A 股主指数不再依赖 AkShare，改由新浪 hq 作为主路径，AkShare 只做增强与补位。

换句话说：**AkShare 在现架构里是“增强层”，不是“主干层”。**

---

## 2. 按信息类型拆解（核心表）

| 信息类型 | 当前函数/入口 | 稳定性评级 | 典型失败 | 当前兜底/策略 |
|---|---|---|---|---|
| A 股主指数 | `stock_zh_index_spot_em`（历史/预留） | 中 | 超时、东财风控、空返回 | 主路径已切新浪 `hq.sinajs.cn`；AkShare 不再做主依赖 |
| 北向资金 | `stock_hsgt_fund_flow_summary_em` | 中 | 超时、返回空、口径异常 | 降级链：Tushare `moneyflow_hsgt` → 新浪 7x24 文本抽取 → RSSHub 文本抽取 |
| 行业强弱 | `stock_fund_flow_industry` | 中上 | 接口抖动、字段变化 | 失败写 `errors`，不阻断主链路 |
| 涨停/跌停计数 | `stock_zt_pool_em` / `stock_dt_pool_em` | 中- | 超时、风控、空表 | 字段允许为空并告警，不影响快照输出 |
| 主力净流入行业 | `stock_fund_flow_industry`（复用） | 中 | 同行业接口 | 若失败则该块为空，摘要提示“尽力探测” |
| 市场热词（情绪） | `stock_hot_tgb` → `stock_hot_keyword_em` | 低 | `stock_hot_tgb` 常缺失 | 可从新闻离线抽词补 `hot_keywords` |
| 热门个股（情绪） | `stock_hot_rank_wc` → `stock_hot_rank_em` | 低到中 | `stock_hot_rank_wc` 常缺失 | 回退东财 `stock_hot_rank_em` |
| 六大板块快讯补源 | `stock_info_global_cls(symbol=重点/全部)` | 中上 | import 失败、字段变化 | 与 RSSHub 快讯合并去重；`cross_source_hit` 标注双源共振 |
| 社媒二级兜底 | `stock_hot_tgb` 或 `stock_hot_rank_em` | 中 | tgb 缺失、网络波动 | 三级降级：微博 API → AkShare → 百度热榜 |

---

## 3. 代码中 AkShare 的真实使用位置

### 3.1 行情与资金（`fetchers/market.py`）

主要调用：

- `stock_hsgt_fund_flow_summary_em`（北向主探测）
- `stock_fund_flow_industry`（行业强弱 + 主力净流入）
- `stock_zt_pool_em` / `stock_dt_pool_em`（涨跌停计数）
- `stock_hot_tgb` / `stock_hot_keyword_em` / `stock_hot_rank_wc` / `stock_hot_rank_em`（情绪热词与热股）

关键事实：

- A 股三大指数主链路已经是新浪，AkShare 扩展探测可整体关闭：
  `FINANCE_SOURCE_SKIP_AKSHARE_PROBE=1`
- AkShare 扩展部分全部是“尽力探测”，失败写 `errors`，不应导致整次快照失败。

### 3.2 快讯（`fetchers/news_rss.py`）

AkShare 仅在财联社快讯补源使用：

- `stock_info_global_cls(symbol="重点")`
- `stock_info_global_cls(symbol="全部")`

策略：

- 先走 RSSHub 多路由，再融合 AkShare 财联社快讯。
- 合并后使用标题归一化去重，并标记 `cross_source_hit`（双源共振）。

### 3.3 社媒（`fetchers/social_api.py`）

AkShare 是微博 API 失败后的第二梯队：

- 优先 `stock_hot_tgb`，不存在则回退 `stock_hot_rank_em`
- 结果转成统一 `title/clean_text/platform`
- 若仍失败，继续降级到百度热榜（第三梯队）

---

## 4. 当前“成功 / 不稳定 / 失败”分层说明

## 4.1 成功或相对稳定（在当前环境有持续产出）

1. `stock_info_global_cls`（财联社快讯）
   - 在现网可参与六大板块内容，且与 RSSHub 可形成双源互证。
   - 风险：字段名变化会影响解析（如“标题/内容/发布时间/发布日期”）。

2. `stock_fund_flow_industry`（行业与主力净流入）
   - 常能产出行业涨跌与净流入排行。
   - 风险：交易时段与接口抖动会导致间歇空数据。

3. `stock_hot_rank_em`（东财人气榜）
   - 当前是社媒/热股最可靠的 AkShare 入口。
   - 风险：只能告诉“谁热”，语义深度不足。

## 4.2 不稳定（可用但波动大）

1. `stock_hsgt_fund_flow_summary_em`（北向）
   - 有时可用，有时空或超时，且交易时段口径不一致。
   - 已用 Tushare / 文本抽取建立降级链，避免北向字段常空。

2. `stock_zt_pool_em` / `stock_dt_pool_em`（涨跌停池）
   - 易受接口可用性和超时影响。
   - 当前允许为空并保留告警，避免拖垮整体快照。

3. `stock_hot_keyword_em`（热词）
   - 作为 `stock_hot_tgb` 的补位，命中质量随行情主题波动。
   - 当前会和离线新闻抽词共同兜底。

## 4.3 当前失败或基本不可用

1. `stock_hot_tgb`
   - 典型错误：`AttributeError('akshare has no stock_hot_tgb')`
   - 说明：当前版本常无该函数，属于版本兼容问题。

2. `stock_hot_rank_wc`
   - 典型错误：`AttributeError('akshare has no stock_hot_rank_wc')`
   - 说明：问财热榜接口在当前 AkShare 版本不可用或已变更。

这两项失败属于“预期内失败”：系统已有回退，不会影响主链路成功返回。

---

## 5. 现网错误码与含义（AkShare 相关）

在 `errors` 中常见这些 code：

- `AKSHARE_IMPORT_ERROR`：环境里缺少 `akshare`（或导入失败）
- `AKSHARE_PROBE_SKIPPED`：主动关闭了 AkShare 扩展探测
- `NORTHBOUND_PROBE_FAILED`：AkShare 北向主探测失败
- `INDUSTRY_RANK_PROBE_FAILED`：行业排行探测失败
- `TEMPERATURE_POOL_PROBE_FAILED`：涨跌停池探测失败
- `INFLOW_SECTOR_PROBE_FAILED`：主力净流入行业探测失败
- `TGB_HOT_FAILED`：淘股吧热榜函数不可用/失败
- `WC_RANK_FAILED`：问财热榜函数不可用/失败
- `EM_KEYWORDS_FAILED`：东财热词失败
- `EM_RANK_FAILED`：东财人气榜失败
- `CLS_AKSHARE_IMPORT_FAILED`：财联社 AkShare 模块不可用

---

## 6. 为什么我们把 AkShare 放到“增强层”

核心原因是部署现实而非代码风格：

1. 云主机公网 IP 易被东财/同花顺识别，WAF 风险高。  
2. AkShare 函数命名和可用性会随版本变化。  
3. 金融场景不能接受“主指数都依赖高波动源”。

所以当前策略是：

- **主干稳定**：新浪 hq + RSSHub + Tushare + 政策官网
- **增强补位**：AkShare（资金、行业、热度、财联社）
- **失败可见**：errors 明确输出，绝不静默“装作有数据”

---

## 7. 建议你在总文档里这样写 AkShare（一段可直接复用）

“AkShare 在本系统中并非单一路径，而是分模块使用：在行业资金、财联社快讯、东财人气榜等维度具备可用性；在淘股吧与问财热榜维度存在当前版本函数缺失；在北向、涨跌停等维度存在云端网络与 WAF 引发的波动。基于此，系统已将 AkShare 调整为增强层数据源，并通过新浪主指数、Tushare 降级、RSSHub 文本抽取及错误码显式告警构建容错链，确保整体快照稳定输出。”

---

## 8. 后续优化清单（仅针对 AkShare）

1. 维护 AkShare 版本兼容矩阵（函数可用性 + 字段名快照）。
2. 对高失败函数（`stock_hot_tgb`、`stock_hot_rank_wc`）降级为“可选实验源”，减少噪音告警。
3. 为 AkShare 探测增加独立健康分数，写入 `meta`（非事实字段）。
4. 对行业、北向、涨跌停接口建立“交易时段感知”阈值，降低非交易时段误报。
5. 若后续引入商业行情 API，可将 AkShare 完全转为“补充特征源”。
