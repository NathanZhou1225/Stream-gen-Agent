# 信源试错与错误总结（v0.1）

> **文档作用**：复盘从设计 `streamy` 内容 agent 到当前版本为止，信源侧经历过的主要试错、失败原因、修复路径，以及为什么最终演进成现在的 `finance-source-ingest` 架构。  
> **读者**：产品、接手工程、未来继续优化信源质量的人。  
> **最后更新**：2026-04-30  
> **相关文件**：`../skills/finance-source-ingest/`、`../skills/streamy-content-gen/`、`../memory/rules/MEMORY_ingest_source.md`

---

## 1. 一句话结论

信源侧最大的教训是：**金融事实不能靠 Agent 临场搜索、prompt 自觉、或单一爬虫源兜底；必须做成可脚本化、可降级、可审计的独立信源层，并把失败显式写进 `errors`。**

当前形态之所以比早期稳定，是因为已经把信源拆成了几层：

- `finance-source-ingest`：只负责拉事实，输出单 JSON，不调 LLM、不写 Draft。
- `streamy-content-gen`：只消费事实包，生成选题/大纲/逐字稿。
- `query_market_facts.py`：用户纯拉数时的统一入口。
- `preflight_topic.py`：带方向开稿前的轻量事实编排入口。
- RSSHub / 新浪 / AkShare / Tushare / 政策官网等：按可靠性分层，而不是押单一源。

---

## 2. 初始设计阶段：把“有数据源”想得太简单

### 2.1 当时的设想

早期 v0.1 范围里，信源被定义为“选题前给 Agent 一些当天真实数据”，最初路线是：

- Tushare / AkShare 提供行情、指数、行业。
- 热榜脚本提供社会热点。
- 财联社先 stub，占位后续快讯。
- Agent 在 `streamy-content-gen` 里按三阶段流程消费这些数据。

这个设计的优点是范围小、能快速跑通内容生成主链路；但它低估了金融信源在真实云端和飞书环境中的不稳定性。

### 2.2 暴露的问题

- **数据源与生成流程耦合**：content-gen 既要拉数据又要写稿，职责开始混在一起。
- **信源深度不够**：只有指数/热榜时，选题能成立，但缺少“为什么今天值得讲”的快讯和政策背景。
- **财联社 stub 不够用**：占位能过开发，但飞书实测时用户感知是“没有真正拉信源”。
- **Agent 会省略信源展示**：即使脚本拉了数据，Agent 也可能只给三条标题，用户看不到事实锚点。

### 2.3 解决方式

- 把信源能力拆成独立 skill：`finance-source-ingest`。
- 约定 stdout 只输出一个 JSON，含 `sections`、`errors`、`markdown_summary`。
- 用 `source_context` / `evidence_anchor` 接入 `topic_picking`，避免 Agent 手写“看起来像事实”的候选。
- 在带方向开稿前新增 `preflight_topic.py`，由它拉信源并构造候选 payload。

---

## 3. 飞书实测阶段：Agent 会“拉了但不展示”

### 3.1 错误现象

进入飞书实机后，出现过几类用户侧红灯：

- 用户让“看今天热点/行情”，Agent 只发一个简短表格或几条标题。
- 带方向开稿时，Agent 拉完事实后直接跳到大纲/逐字稿，绕过选题确认。
- 有 digest 但不展示，用户误以为没有拉信源。
- 三个候选标题像同一句话的“解读/影响”变体，缺少差异化。

### 3.2 失败原因

根因不是单个 API，而是 Agent 编排边界不清：

- 纯拉数、带方向开稿、正式写稿混成一条路径。
- prompt 里要求“展示信源”，但模型会为了省篇幅省掉。
- `markdown_summary` 太长时，Agent 倾向抽一小段甚至重写。
- 候选生成如果交给 LLM 自由发挥，容易把用户原话包装成三条伪候选。

### 3.3 解决方式

- 纯拉数统一走：
  `python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --max-items 30`
- 纯拉数时必须原样展示 JSON 里的 `markdown_summary`。
- 带方向开稿时走 `preflight_topic.py`，同轮只允许落盘 `topic_picking` 和给短回复。
- `feishu_digest_bullets` 单独输出，控制飞书可见摘要长度。
- 候选标题从 `markdown_summary` 规则行生成，过滤告警/降级噪声，不再允许“用户原句 + 解读/影响”。

---

## 4. 信源内容阶段：从“有快讯”到“有结构”

### 4.1 错误现象

早期 `markdown_summary` 有过几个问题：

- 展示顺序不稳定，用户说“行情/热点/全量”会触发不同结构。
- 只有大盘或快讯半截，缺少一眼可读的全量快照。
- 大事件混入午评、涨停分析、公司业绩、IPO 等日常市场新闻。
- 六大板块里经常出现“快讯不足”式占位，用户感知像没拉到数据。
- 社媒区只有标题，没有“概述”，无法解释为什么热。

### 4.2 失败原因

- 过早按用户措辞拆 `--sources`，导致“今日行情”和“今日热点”不是同一套事实包。
- 快讯按单一池子过滤，板块命中不足时没有跨源补位。
- 大事件规则太宽，把公司新闻和宏观/政策/地缘大事混在一起。
- 社媒源本身弱，且没有结构化摘要。

### 4.3 解决方式

最终确定纯拉数一律全量跑 `market,news,social`，并固定输出顺序：

1. 大盘与情绪
2. 六大核心板块异动
3. 大事件
4. 今日热点讯息
5. 深度内容
6. 社媒 / 人气榜
7. 中文告警

随后做了几轮结构增强：

- 三大指数优先，再北向、行业、资金、情绪。
- 大事件只保留国家/全球/政策/地缘/峰会类，剔除公司日常新闻。
- 六大板块从 `news`、`global_macro`、`deep_news` 全池合并，板块内去重，保留正文更长版本。
- 板块 0 条时才用行情侧补充，不再默认展示“快讯不足”。
- 今日热点从非六大板块池中精选 5 条，避免和大事件/板块内容重复。
- 社媒区补 `clean_text` 概述。

---

## 5. 云端部署阶段：东财 / AkShare 不能当唯一主源

### 5.1 错误现象

云端和火山/ECS 环境里，AkShare 相关路径出现过不稳定：

- 东财/同花顺接口容易被 WAF、限流或远端断连影响。
- 部分 AkShare 函数随版本变化消失，例如 `stock_hot_tgb`、`stock_hot_rank_wc`。
- 北向、行业、涨跌停、情绪等扩展字段可能空或超时。
- 复杂爬虫封装在云主机上比本地更脆。

### 5.2 失败原因

- AkShare 是优秀的聚合库，但其底层很多接口仍依赖目标网站结构和反爬策略。
- 云主机 IP 更容易被金融网站识别为异常访问。
- 部分接口没有稳定超时参数，需要自己包线程超时。
- 把 AkShare 作为行情唯一主源，会让基础指数也被 WAF 风险拖累。

### 5.3 解决方式

- A 股三大指数主路径改为新浪财经 `hq.sinajs.cn`。
- 港股指数也走新浪 hq。
- AkShare 改为“增强探测”：北向、行业、涨跌停、资金、人气榜尽力补充。
- 增加 `FINANCE_SOURCE_SKIP_AKSHARE_PROBE=1`，在 WAF 严重环境里可关闭扩展探测，仅保留指数主链路。
- 北向资金做分级降级：AkShare → Tushare → 新浪 7x24 文本 → RSSHub 文本。
- 所有失败写入 `errors`，不再静默吞掉。

---

## 6. 联网搜索阶段：WebSearch / Tavily 不能混进事实 JSON

### 6.1 试过的方案

有一段时间，系统尝试把缺口结构化为：

- `meta.websearch_required`
- `meta.websearch_gaps`
- Agent 必须调用 WebSearch / Tavily 补充

也尝试过让 `query_market_facts` 拼接 Tavily 搜索附录。

### 6.2 错误现象

- Agent 有时只复述“需要 WebSearch”，但不真的搜索。
- 搜索结果和 API 数值事实混在一起，边界不清。
- `markdown_summary` 容易出现“脚本事实 + Agent 搜索补充”的混合文本，审计困难。
- Tavily skill 若放到默认 workspace，容易被 main 等非 stream-gen agent 看到，与“仅 stream-gen 使用”冲突。
- SearXNG 方案曾尝试，但最终确认当前环境无法实现，已移除。

### 6.3 解决方式

- ingest 默认链路彻底移除 Tavily / WebSearch 附录。
- 不再输出 `meta.websearch_required` / `meta.websearch_gaps`。
- 缺口只写 `errors` 和 `markdown_summary` 中文告警。
- Agent 可以在用户追问时自愿联网补充，但不得改写或替代 JSON 的 `sections` 数值事实。
- Tavily 仅保留在 `workspace-stream-gen/skills/liang-tavily-search-1.0.1/`，不放入默认 workspace skill 列表。

---

## 7. 新闻与深度阶段：RSSHub 成为关键缓冲层

### 7.1 错误现象

直连媒体源出现过多种失败：

- 华尔街见闻直连 API 返回空条。
- 第一财经 RSS 候选 URL 返回 404。
- 金十直连 RSS 受 DNS/出网影响。
- 界面直连 RSS 返回 404。
- 财经快讯深度不足，六大板块常缺条目。

### 7.2 失败原因

- 媒体站 API/RSS 路由变化频繁。
- 部分站点对云端请求不友好。
- 每个站点单独适配维护成本高。
- 直连失败时，如果没有统一缓冲层，会导致新闻区整体塌陷。

### 7.3 解决方式

- 引入 `FINANCE_RSSHUB_BASE_URL`，优先走自建 RSSHub。
- news 层重点用 `wallstreetcn/live`、`jin10`、`36kr/newsflashes`。
- deep_news 层用 RSSHub 前置，再回退直连华尔街见闻/第一财经/金十/界面。
- 增加 `repair-rsshub` 子命令和回调服务，支持飞书/微信按钮授权修复 RSSHub。
- RSSHub 路由失败时写 `errors`，并在非交互环境提示人工修复，不让脚本卡住。

---

## 8. 社媒阶段：当前仍是最弱环

### 8.1 错误现象

社媒/人气区目前仍有明显短板：

- 微博热搜 API 实测 DNS 失败。
- 自爬社媒仍是 stub。
- 东财人气榜能给股票热度，但缺少语义解释。
- 百度热榜有时只有少量泛财经命中。

### 8.2 当前处理方式

- 第一梯队：vvhan 微博热搜。
- 第二梯队：AkShare 淘股吧/东财人气榜。
- 第三梯队：百度实时热搜。
- 如果前一梯队失败，自动降级，并写入 `errors`。

### 8.3 仍未解决

社媒还没有达到“可解释热点”的程度。当前只能说“谁热”，很难稳定回答“为什么热、和市场有什么关系、是否值得做选题”。

下一步更适合接一个稳定第三方 JSON 热榜/社媒 API，字段至少包括：

- 标题
- 平台
- 热度
- 摘要或上下文
- 链接
- 时间
- 主题标签

---

## 9. 最终达到的当前程度

截至 2026-04-30，信源链路已经达到以下状态：

- **可跑通**：本机 `query_market_facts.py --sources market,news,social --max-items 30` 可输出完整快照。
- **可展示**：`markdown_summary` 已形成固定全量结构，适合飞书直接粘贴。
- **可审计**：原始事实在 `sections`，失败在 `errors`，摘要不作为补数依据。
- **可降级**：新浪主指数、AkShare 增强、Tushare 北向、RSSHub 快讯、社媒多级降级。
- **可迁移**：`finance-source-ingest` 独立 skill，subprocess 调用，不依赖 content-gen import。
- **可控边界**：不调 LLM、不写 Draft、不把 Tavily/Search 混入事实 JSON。
- **可运维**：RSSHub 有修复入口；AkShare 可用环境变量跳过；venv 可自动自举。

这不是“所有信源都稳定”的状态，而是“主链路稳定、失败显式、弱源降级、事实边界清楚”的状态。

---

## 10. 错误清单与当前解法

| 错误 / 误判 | 表现 | 当前解法 |
|-------------|------|----------|
| 把信源当成 content-gen 内部小脚本 | 数据和写稿职责混杂 | 拆出 `finance-source-ingest` 独立 skill |
| 以为 prompt 会保证 Agent 展示信源 | 飞书只给标题，不给事实锚点 | 纯拉数强制原样贴 `markdown_summary`，开稿走 `feishu_digest_bullets` |
| 以为 AkShare 能作为云端唯一行情主源 | 东财 WAF、函数变更、超时 | 新浪 hq 做指数主链路，AkShare 只做增强探测 |
| 以为联网搜索能自动兜底事实缺口 | WebSearch 不执行、事实边界混乱 | 移除 `meta.websearch_*` 与 Tavily 附录，缺口进 `errors` |
| 以为直连媒体 RSS/API 可长期稳定 | 404、空返回、DNS 失败 | RSSHub 前置，直连仅回退 |
| 以为“有快讯”就够 | 六大板块空、热点重复、大事件混乱 | 全池合并、板块去重、大事件白名单、固定展示结构 |
| 以为社媒热榜能直接用 | 只有热度，没有原因 | 当前多级降级；后续需稳定第三方 JSON 社媒 API |
| 以为失败少展示更美观 | 用户无法判断是否拉源 | 中文告警保留，最多展示关键失败 |

---

## 11. 后续优化原则

1. **主链路只选云端友好源**：新浪 hq、RSSHub、本地缓存、明确 key 的 API 优先。
2. **增强源可以不稳定，但不能拖垮主链路**：AkShare、社媒、直连 RSS 都应失败可见、可跳过。
3. **搜索和事实分离**：搜索可以解释缺口，但不能覆盖 API 数值。
4. **摘要和原始事实分离**：`markdown_summary` 给人看，`sections/errors` 才是机器和审计依据。
5. **飞书展示要固定结构**：不要让 Agent 根据用户措辞临场改输出形态。
6. **每个失败都要能定位**：`errors[].source/stage/code/message/hint` 比“暂无数据”更重要。

---

## 12. 当前推荐基线

火山 / ECS 部署建议至少具备：

- `FINANCE_RSSHUB_BASE_URL=http://127.0.0.1:1200`
- `TUSHARE_TOKEN=<token>`（用于北向和政策增强）
- 可出网安装 `akshare`、`feedparser`、`tushare`
- 必要时启用 `FINANCE_SOURCE_SKIP_AKSHARE_PROBE=1`
- 保留 `repair-rsshub` 回调链路

OpenClaw / Agent 使用建议：

- 纯拉数只调用 `query_market_facts.py`。
- 带方向开稿只调用 `preflight_topic.py`，同轮不越级写大纲/逐字稿。
- 对用户展示以 `markdown_summary` 或 `feishu_digest_bullets` 为准。
- 不把 Tavily / WebSearch 结果写回 ingest JSON。
- 信源质量问题优先读 `memory/rules/MEMORY_ingest_source.md`。
