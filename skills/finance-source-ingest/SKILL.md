---
name: finance-source-ingest
description: |
  独立金融信源聚合：AkShare 行情（A 股指数、北向、行业强弱）、可选新浪海外/大宗备源、
  六大板块快讯（**必选** `FINANCE_RSSHUB_BASE_URL`：RSSHub `wallstreetcn/live` + `jin10` + `36kr/newsflashes`，feedparser + 关键词过滤）、新浪7x24全球宏观、证监会/央行公告（可选 TUSHARE_TOKEN+CCTV）、深度层（同 RSSHub 基址 + 直连华尔街见闻/第一财经/金十/界面）、社媒热点（微博热搜 → AkShare 淘股吧/东财 → 百度热搜降级）；可选第三方 JSON；输出单 JSON + markdown_summary，不调 LLM、不写 drafts、不内嵌 Tavily。
  OpenClaw 无单独「激活」开关：出现在技能列表即可用；仅需 Python 依赖（akshare、feedparser）与可选环境变量增强数据，不要求 Tushare/财联社 key 作为本 skill 前提。
  当用户需要「拉一手实时数据 / 信源快照 / 选题前事实包」且不想只依赖 streamy-content-gen 内嵌 fetch 时启用本 skill。
  典型触发词：信源、行情快照、AkShare、拉数据、RSS、热点 API、finance-source-ingest、ingest run。
  不触发：已明确只要产出逐字稿且走 streamy-content-gen 全流程时，可继续只用 streamy-content-gen。
---

# finance-source-ingest

## 何时启用

用户或上游 Agent 需要 **当日/实时金融事实**（A 股指数、北向、行业强弱、可选海外、RSSHub 六大板块快讯、社媒热点多级降级或第三方热点 JSON）用于选题或事实锚点，且不希望通过 `streamy-content-gen` 内嵌脚本耦合时。

## OpenClaw：没有「点一下激活」

- 本 skill **不是**「未配置 key = 未安装」。只要网关/会话的 `<available_skills>` 里已有 `finance-source-ingest`，**skill 即视为可用**。
- **必须做的**只有：部署机能执行 `ingest.py`（需一次出网以装依赖）。**换目录 / 迁服务器后**勿拷贝旧 `.venv`；首次用 `python3 scripts/ingest.py` 会**自动**建 `.venv` 并 `pip install -r` 再重载（可 `FINANCE_INGEST_NO_AUTO_VENV=1` 关闭，见 README）。亦可用 **`./scripts/bootstrap_venv.sh`** 手跑。`preflight_topic` 会优先使用兄弟目录 `finance-source-ingest/.venv/bin/python`（无则 `python3` 会触发自举直到 `.venv` 存在）。
- **快讯门禁**：`FINANCE_RSSHUB_BASE_URL`（如 `http://127.0.0.1:1200`，无尾斜杠）自建 RSSHub；`news` 源依赖 **feedparser** 拉取 `wallstreetcn/live`、`jin10`、`36kr/newsflashes` 并做关键词白名单过滤。**不配则 `sections.news` 为空**并产生告警。深度层仍可用同基址优先 RSSHub 再回退直连。其它可选：`FINANCE_SOURCE_OVERSEAS_STUB=1`；`FINANCE_SOURCE_SOCIAL_API_URL`；`TUSHARE_TOKEN` + `tushare` 用于 CCTV。行情主路径 **AkShare 不需要 Tushare**。

## 调用契约

0. **stream-gen 用户侧纯拉数（P0）**：若当前 workspace 是 `workspace-stream-gen`，且用户只要「拉今日信源 / 今日行情 / 今日热点 / 全量信息」，推荐用包装脚本（与 `ingest.py` 同一 JSON，便于统一加载 `.env`）：
   - `python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --max-items 30`
   - 该包装**仅**调用本 skill 并原样输出 JSON，**不再**拼接 Tavily 或其它联网附录。
   - 直接调用 `ingest.py` 仍用于底层调试、CI、或其它编排。
1. 在 workspace 下推荐路径（与 OpenClaw 软链一致）：
   - `cd $OPENCLAW_WORKSPACE`（一般为 `workspace-streamy`）
   - `.venv/bin/python skills/finance-source-ingest/scripts/ingest.py run --sources market,news,social ...`
   - 或在 `skills/finance-source-ingest/scripts/` 内：`../.venv/bin/python ingest.py run ...`（venv 建在 skill 根目录时）
2. **stdout 为单个 JSON 对象**，含 `sections`、`errors`、`markdown_summary`。
3. **事实以 `sections` 与 `errors` 为准**；`markdown_summary` 仅辅助阅读，不得从中补造数字。配置 `FINANCE_RSSHUB_BASE_URL` 时，`sections.deep_news` 可含 **`sector_rsshub_matrix`**（六大板块垂直路由：每板块 `routes_tried_detail` / `routes_ok` / `routes_failed` / `items_count`），`meta.deep_news_sector_rsshub` 与之相同便于对拍。
4. **`markdown_summary` 结构**：大盘与情绪（三大指数优先 → 北向资金 → 其他情绪/资金）→ 六大板块 **RSSHub 快讯**摘要（**每条带时间**；板块内不足时用宽池关键词回溯；仍无新闻时用行情侧补充或明确暂无；**深度层条目已并入各板块展示，无独立「深度内容」小节**）→ 大事件（国家/全球/政策/地缘/峰会类，不放公司业绩/午评/涨停分析）→ **全球宏观**（新浪7x24 + 证监会/人民银行 + 可选 CCTV）→ 今日热点讯息（仅金融相关）→ 社媒/人气榜探测 → 中文告警。机器侧完整深度列表仍以 JSON **`sections.deep_news`** 为准。与上游 Agent 的「只发指数表」类输出**不**同义。
5. **缺口与告警**：接口失败、字段为空、板块未命中等情况写入 `errors` 并在 `markdown_summary` 的告警区用中文说明；**不再**输出 `meta.websearch_required` / `meta.websearch_gaps`，也不内嵌联网检索。
6. **边界**：本 skill 为可迁移 API 信源层，**不调用** Agent WebSearch / Tavily。若上游 Agent 仍需人工联网核对，由 Agent 在对话中自行处理，且不得改写本 JSON 的 `sections` 数值事实。
7. **T3 Router（v0.1.9）**：在组装前会对压缩菜单做单次 LLM 路由：每板块 **1～3 条**、优先深度逻辑、无重大事件时保留【强相关】行业/盘面动态，**仅**当菜单中完全无该板块相关信息时才 `[]`；去重同一事件多源快讯。结果写入 `sections.llm_router.items_by_sector`，主状态写入 `meta.llm_router_status`。超时/网关错/JSON 解析失败时回退 legacy，并写 `errors[].code=LLM_ROUTER_FAILED`。

## 铁律

- 不写 `drafts/`，不调 `draft_manager.py`。
- **指数主路径**：新浪财经 `hq.sinajs.cn` 三大指数；在此之后**默认仍会尝试** AkShare 探测北向/行业/涨跌停/情绪（失败则字段为空并记入 `errors`，不静默跳过）。若部署机对东财 **WAF 极严** 可设 **`FINANCE_SOURCE_SKIP_AKSHARE_PROBE=1`** 关闭扩展探测（仅指数）。
- 海外新浪块仅当 `FINANCE_SOURCE_OVERSEAS_STUB=1`。
- 社媒自爬 v0.1 不可用；无 `FINANCE_SOURCE_SOCIAL_API_URL` 时使用 **微博热搜 → AkShare → 百度热搜** 多级降级；自爬仍仅占位。

## 与 streamy-content-gen

v0.1 **不自动串联**；后续由集成方案定义（例如在 topic 阶段 shell 调用本 CLI 并将 JSON 注入 `source_context`）。

上游 Agent 迁移时：`finance-source-ingest` 与 `query_market_facts.py` 负责可脚本化信源；联网核对由 Agent 策略自行定义，不再与本 skill 输出强耦合。
