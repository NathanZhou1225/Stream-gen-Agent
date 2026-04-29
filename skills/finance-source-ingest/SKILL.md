---
name: finance-source-ingest
description: |
  独立金融信源聚合：AkShare 行情（A 股指数、北向、行业强弱）、可选新浪海外/大宗备源、
  MVP 财联社快讯（AkShare）、社媒热点（无 URL 时：微博热搜 API → AkShare 淘股吧/东财人气榜降级）；可选第三方 JSON；输出单 JSON + markdown_summary，不调 LLM、不写 drafts。
  OpenClaw 无单独「激活」开关：出现在技能列表即可用；仅需 Python 依赖（akshare、feedparser）与可选环境变量增强数据，不要求 Tushare/财联社 key 作为本 skill 前提。
  当用户需要「拉一手实时数据 / 信源快照 / 选题前事实包」且不想只依赖 streamy-content-gen 内嵌 fetch 时启用本 skill。
  典型触发词：信源、行情快照、AkShare、拉数据、RSS、热点 API、finance-source-ingest、ingest run。
  不触发：已明确只要产出逐字稿且走 streamy-content-gen 全流程时，可继续只用 streamy-content-gen。
---

# finance-source-ingest

## 何时启用

用户或上游 Agent 需要 **当日/实时金融事实**（A 股指数、北向、行业强弱、可选海外、财联社快讯、社媒热点多级降级或第三方热点 JSON）用于选题或事实锚点，且不希望通过 `streamy-content-gen` 内嵌脚本耦合时。

## OpenClaw：没有「点一下激活」

- 本 skill **不是**「未配置 key = 未安装」。只要网关/会话的 `<available_skills>` 里已有 `finance-source-ingest`，**skill 即视为可用**。
- **必须做的**只有：部署机能执行 `ingest.py`（需一次出网以装依赖）。**换目录 / 迁服务器后**勿拷贝旧 `.venv`；首次用 `python3 scripts/ingest.py` 会**自动**建 `.venv` 并 `pip install -r` 再重载（可 `FINANCE_INGEST_NO_AUTO_VENV=1` 关闭，见 README）。亦可用 **`./scripts/bootstrap_venv.sh`** 手跑。`preflight_topic` 会优先使用兄弟目录 `finance-source-ingest/.venv/bin/python`（无则 `python3` 会触发自举直到 `.venv` 存在）。
- **可选做的**（增强数据，不是门禁）：`FINANCE_SOURCE_OVERSEAS_STUB=1`；`FINANCE_SOURCE_SOCIAL_API_URL` 配置后走自定义 JSON，**不配则走内置** `fetch_social_trends()`。新闻 MVP 走 AkShare 财联社电报，**不要求** `news_sources.json` / RSS。行情主路径 **AkShare 不需要 Tushare**；也**不要求**财联社 token（那是 streamy-content-gen 里另一脚本的事）。

## 调用契约

1. 在 workspace 下推荐路径（与 OpenClaw 软链一致）：
   - `cd $OPENCLAW_WORKSPACE`（一般为 `workspace-streamy`）
   - `.venv/bin/python skills/finance-source-ingest/scripts/ingest.py run --sources market,news,social ...`
   - 或在 `skills/finance-source-ingest/scripts/` 内：`../.venv/bin/python ingest.py run ...`（venv 建在 skill 根目录时）
2. **stdout 为单个 JSON 对象**，含 `sections`、`errors`、`markdown_summary`。
3. **事实以 `sections` 与 `errors` 为准**；`markdown_summary` 仅辅助阅读，不得从中补造数字。
4. **`markdown_summary` 结构**：大盘与情绪（三大指数优先 → 北向资金 → 其他情绪/资金）→ 六大板块财联社摘要（**每条带时间**；板块内不足时优先用更宽财联社池做关键词回溯；仍无新闻时用行情侧补充或明确暂无）→ 大事件（国家/全球/政策/地缘/峰会类，不放公司业绩/午评/涨停分析）→ 今日热点讯息（仅金融相关）→ 社媒/人气榜探测 → 中文告警；百度实时热榜只展示有财经详情的条目，不展示非财经或无详情标题。与上游 Agent 的「只发指数表」类输出**不**同义。
5. **缺口暴露**：当北向资金为空/返回 0、六大板块无财联社正文、泛财经无可展示详情、社媒/人气榜为空或接口失败、国家/全球大事件未命中时，顶层 `meta.websearch_required: true` 与 `meta.websearch_gaps` 会列出应由 Agent 联网补充的 `area` 与 `reason`。
6. **WebSearch 边界**：本 skill 为可迁移 API 信源层，**不调用 Agent WebSearch**、不依赖 Cursor 工具。若上游 Agent 具备 WebSearch，必须读取本 JSON 的 `meta.websearch_required` / `meta.websearch_gaps` / `markdown_summary` 中的缺口，在用户回复里追加独立「联网补充」；该补充不得写回本 skill 的原始快照，也不得覆盖 `sections` 中的数值事实。

## 铁律

- 不写 `drafts/`，不调 `draft_manager.py`。
- **指数主路径**：新浪财经 `hq.sinajs.cn` 三大指数；在此之后**默认仍会尝试** AkShare 探测北向/行业/涨跌停/情绪（失败则字段为空并记入 `errors`，不静默跳过）。若部署机对东财 **WAF 极严** 可设 **`FINANCE_SOURCE_SKIP_AKSHARE_PROBE=1`** 关闭扩展探测（仅指数）。
- 海外新浪块仅当 `FINANCE_SOURCE_OVERSEAS_STUB=1`。
- 社媒自爬 v0.1 不可用；无 `FINANCE_SOURCE_SOCIAL_API_URL` 时使用 **微博热搜 + AkShare** 多级降级；自爬仍仅占位。

## 与 streamy-content-gen

v0.1 **不自动串联**；后续由集成方案定义（例如在 topic 阶段 shell 调用本 CLI 并将 JSON 注入 `source_context`）。

上游 Agent 迁移时建议成套迁移：`finance-source-ingest` 负责原始信源；`AGENTS.md` / `MEMORY.md` / `streamy-content-gen/prompts/natural-language-intent.md` 负责 WebSearch 兜底展示协议。这样保持本 skill 可单独部署，同时保留飞书侧“信息不足则联网补充”的体验。
