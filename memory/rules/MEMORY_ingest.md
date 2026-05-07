# MEMORY 碎片 · 信源快照（按需读取）

> 触发：用户要**今日行情、热点、快讯、信源快照、全量信息**且未进入手写复盘；或执行 `query_market_facts` / `ingest` 相关。**读完回到主任务，勿一次性混入开稿长流程（开稿规则见 `MEMORY_workflow.md`）。**
> 信源成败、失败原因、盲区、火山引擎部署适配性与 OpenClaw 契合度详见 `MEMORY_ingest_source.md`。

---

## 4.1 行情 / 热点 / 信源快照（与 `finance-source-ingest` 对齐）

当用户要**今日行情、今日热点、快讯、信源快照、全量信息**等（**未**进入带方向开稿链、**未**要求你手写复盘报告）时：

1. **唯一信源形态**：在 workspace 下执行 **`python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --max-items 30`**（内部等价于跑 `finance-source-ingest` 的 `ingest.py`，**不**再自动调用 Tavily）。
2. **展示契约**：将 stdout JSON 里的 **`markdown_summary` 全文原样**发给用户（可加一行采集时间）；**禁止**只贴三大指数表、**禁止**只贴六大板块/快讯表、**禁止**按用户口头词只跑 `market` 或 `news`、**禁止**把「行情」和「热点」拆成两条消息或两套结构。
3. **结构说明**：`markdown_summary` 已内建 **大盘与情绪（三大指数优先 → 北向资金 → 其他情绪/资金）→ 六大核心板块快讯 → 大事件 → 全球宏观 → 金融相关今日热点 → 社媒/人气榜 → 深度内容 → 中文告警**；缺口以 `errors` 与告警区中文说明为准，**无** `meta.websearch_gaps` 自动清单。
4. **旧版五段式**（大盘→焦点→新闻摘要→海外→选题）**仅**保留给：用户明确要求「写一份你手搓的盘面解读/复盘」、或对标账号分析/复盘技能等 **非 ingest 快照** 场景；与 §4.1 本条 **不**混用。

### 4.1A 可选：用户追问时的联网核对（Agent 层）

若用户**主动**要求核实北向、社媒、某板块等，且当前会话具备 WebSearch，可在**不修改**已发出的 `markdown_summary` 前提下，在后续消息中自行检索并说明来源；不得覆盖 JSON 里 API 给出的指数、涨跌幅、北向等数值。
