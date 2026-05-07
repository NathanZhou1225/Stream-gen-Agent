# MEMORY 碎片 · 信源专项记录

> 触发：需要评估、排障或改造 `finance-source-ingest` 的信源质量、失败原因、火山引擎部署适配性、或与 OpenClaw 工具链契合度时读取。日常只拉今日快照时优先读 `MEMORY_ingest.md`。

---

## 1. 当前信源体系

`finance-source-ingest` 是独立金融信源聚合层，输出单个 JSON：`sections` / `errors` / `markdown_summary` / `invariants`。它不调 LLM、不写 `drafts/`、不内嵌 Tavily；`streamy-content-gen` 通过 `query_market_facts.py` 或 `preflight_topic.py` 以 subprocess 调用。

默认信源分层：

- 行情：新浪财经 `hq.sinajs.cn` 为 A 股三大指数和港股指数主路径；AkShare / Tushare 尽力补北向、行业、涨跌停、资金流。
- 快讯：自建 RSSHub，重点路由为 `wallstreetcn/live`、`jin10`、`36kr/newsflashes`，并可结合 AkShare 财联社快讯。
- 宏观政策：新浪财经 7x24、证监会、人民银行，可选 Tushare CCTV。
- 深度内容：RSSHub 优先接华尔街见闻、金十、第一财经、界面、36氪；直连 API/RSS 仅作回退。
- 社媒/人气：微博热搜 → AkShare 淘股吧/东财人气榜 → 百度热搜降级；自爬仍为占位。
- 搜索：Tavily 保留为独立 skill，不再进入 ingest 默认链路。

## 2. 当前成功信源

- **新浪财经 hq**：成功。提供 A 股三大指数、港股指数；无需 key，轻量，云端适配性最好。
- **Tushare 北向**：成功。提供 `moneyflow_hsgt` 北向资金；依赖 `TUSHARE_TOKEN`。
- **AkShare / 东财部分接口**：部分成功。行业强弱、主力净流入行业、东财人气榜可用；受 AkShare 版本和东财 WAF 影响。
- **RSSHub 快讯**：成功。`FINANCE_RSSHUB_BASE_URL=http://localhost:1200` 时可支撑六大板块快讯。
- **RSSHub 深度内容**：成功。华尔街见闻、金十、第一财经、界面、36氪路由均可用于深度层。
- **新浪财经 7x24**：成功。适合全球宏观、大事件和北向文本降级。
- **证监会 / 人民银行官网**：成功。提供权威政策公告，但解析依赖网页结构。
- **百度热榜 API**：成功但命中有限。仅作为宏观/泛财经热点补位。
- **财联社 AkShare**：当前可用迹象明确，能参与板块快讯和“双源共振”。

## 3. 当前失败或降级信源

- **微博热搜 vvhan API**：失败；实测报 DNS/出网错误 `Name or service not known`。已降级到 AkShare 东财人气榜。
- **AkShare 淘股吧 / 问财情绪函数**：失败；当前 AkShare 版本缺少 `stock_hot_tgb`、`stock_hot_rank_wc` 等符号。属于库 API 变更或不可用，不阻断主链路。
- **华尔街见闻直连 API**：失败或空；直连 `content/lives`、`content/articles` 返回空条。RSSHub 版仍可用。
- **第一财经直连 RSS**：失败；候选 RSS URL 返回 404。RSSHub 版仍可用。
- **金十直连 RSS**：失败；实测 DNS/出网错误。RSSHub 版仍可用。
- **界面直连 RSS**：失败；直连 RSS 返回 404。RSSHub 版仍可用。
- **Tavily / WebSearch 附录**：已主动退出默认链路；缺口写入 `errors` 与中文告警，由 Agent 自愿补充但不得覆盖 JSON 事实。

## 4. 各类信源包含的信息

- 行情类：A 股三大指数、港股指数、北向资金、行业涨跌、涨跌停、主力净流入行业、市场热词。
- 快讯类：科技、新能源、港股、黄金、有色、银行等六大板块快讯；每条尽量带时间、标题、摘要、来源、情绪和影响标注。
- 宏观政策类：新浪 7x24、证监会、央行公告，可形成“大事件”和“全球宏观”。
- 深度内容类：华尔街见闻、金十、第一财经、界面、36氪，提供较长摘要、影响层级、股票提及和板块标签。
- 社媒/人气类：微博热搜、东财人气榜、百度热榜；当前主要靠东财人气榜兜底。
- 搜索类：Tavily 独立可用，但不得伪装成 ingest 原始信源。

## 5. 主要盲区

- 社媒质量弱：微博热搜不稳，东财人气榜只有股票热度，缺少“为什么热”的语义解释。
- RSSHub 关键依赖重：当前新闻和深度质量高度依赖本地 RSSHub；路由老化会明显拉低质量。
- 直连媒体 RSS/API 不可靠：多处 404、空返回或 DNS 失败，不能作为主路径。
- AkShare 受版本和 WAF 双重影响：适合作为增强源，不适合作为唯一主源。
- 不是自动事实核验系统：能聚合与标源，但不自动解决所有跨源冲突。
- 港股、海外、大宗覆盖有限：港股指数可用，海外/大宗默认不开，需 `FINANCE_SOURCE_OVERSEAS_STUB=1`。

## 6. 火山引擎部署适配性

总体适配性：**中上**。

适合火山/ECS 的部分：

- 新浪 hq 主链路轻量、无需 key、对服务器环境友好。
- RSSHub 本地化后可规避大量直连媒体站不稳定问题。
- `ingest.py` 支持自动 venv 自举，迁机时只需代码和依赖出网。
- 输出单 JSON，便于网关、飞书、脚本和日志排障。

主要部署风险：

- DNS/出网失败会影响微博、金十直连等外部域名。
- 东财/同花顺 WAF 可能封锁云主机 IP，影响 AkShare 扩展行情。
- RSSHub 路由需要维护，目标站改版后可能需要更新 RSSHub。
- 首次部署需要 pip 出网安装 `akshare`、`feedparser`、`tushare`。

推荐环境变量：

- `FINANCE_RSSHUB_BASE_URL=http://127.0.0.1:1200`
- `TUSHARE_TOKEN=<token>` 用于北向、CCTV 等增强
- `FINANCE_SOURCE_SKIP_AKSHARE_PROBE=1` 仅在东财 WAF 严重时启用，用新浪指数保底
- `FINANCE_SOURCE_OVERSEAS_STUB=1` 仅在需要海外/大宗时开启

## 7. OpenClaw 工具链契合度

总体契合度：**高**。

- 与 OpenClaw agent 边界清楚：本 skill 只产事实快照，不写 `drafts/`，不调用 `draft_manager.py`。
- 与 `streamy-content-gen` 弱耦合：通过 subprocess 连接，可用 `--finance-root` 迁移路径。
- 与飞书展示契合：`markdown_summary` 已内建完整展示顺序和中文告警。
- 与工具硬约束策略契合：事实以 `sections` 和 `errors` 为准，`invariants` 明确禁止从摘要补数。
- 与多 agent 迁移契合：不依赖 Tavily 或某个 Agent 的 WebSearch 能力。

## 8. 后续优化优先级

1. **P0：RSSHub 健康检查和路由维护**。保留 `repair-rsshub` 回调链路，必要时增加定时健康检查。
2. **P1：社媒增强**。优先接一个稳定第三方 JSON 社媒/热榜 API，填补“为什么热”的摘要。
3. **P1：AkShare 版本兼容清单**。把已失效函数替换为当前版本可用函数，或将其从告警中降级为已知不可用。
4. **P2：跨源去重和冲突权重**。对同一新闻在财联社、RSSHub、36氪之间做更稳定的主题聚合。
5. **P2：火山部署自检脚本**。一次性检查 DNS、RSSHub、Tushare、AkShare、关键域名出网和版本。
