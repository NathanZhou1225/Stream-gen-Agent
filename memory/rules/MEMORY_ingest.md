# MEMORY 碎片 · 信源快照（按需读取）

> 触发：用户要**今日行情、热点、快讯、信源快照、全量信息**且未进入手写复盘；或执行 `query_market_facts` / `ingest` 相关。**读完回到主任务，勿一次性混入开稿长流程（开稿规则见 `MEMORY_workflow.md`）。**
> 信源成败、失败原因、盲区、火山引擎部署适配性与 OpenClaw 契合度详见 `MEMORY_ingest_source.md`。

---

## 4.1 行情 / 热点 / 信源快照（与 `finance-source-ingest` 对齐）

当用户要**今日行情、今日热点、快讯、信源快照、全量信息**等（**未**进入带方向开稿链、**未**要求你手写复盘报告）时：

1. **唯一信源形态**：在 workspace 下执行 **`python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --summary-only`**（默认走**云端 API** → 本地 Router/Rewriter；**禁止**再跑 `ingest.py run` 或本地 `finance_sources.db`；**不**自动 Tavily）。耗时约 **90–120 秒**，须等命令结束再回复。OpenClaw **`exec` 的 `timeout` 须 ≥ 300**（秒）；`process poll` ≥ **180000** ms。  
   **快照缓存（开稿提速）**：成功时脚本会自动写入 **`workspace-stream-gen/cache/snapshot/snapshot.json`**（**完整** snapshot JSON，含 `sections` + `markdown_summary`）。与 `preflight_topic.py` 共用；**同一 workspace 目录**下所有会话复用，**不是**每个 OpenClaw agent 各一份（WorkBuddy 若 clone 另一份 workspace 则有独立缓存）。stdout `meta.snapshot_cache_path` 可传给后续 `--snapshot-path`。`--no-write-cache` 可关闭。
2. **展示契约**：将 stdout JSON 里的 **`markdown_summary` 全文原样**发给用户（可加一行采集时间）；**禁止**自编「六大板块 / 热点精选 / Top3 表格」等二次排版（脚本已是 **🔹 + 事件/影响/角度** 四行结构）。**禁止**只贴三大指数、**禁止**拆成两条消息。**用户明确要求「再展示一次全量快照」时**：须 **重新执行** `query_market_facts`（约 1～2 分钟），再 **全文粘贴** 新的 `markdown_summary`，不得以「本会话已展示过」为由缩写或表格化复述。
3. **禁止**主动提议「实时刷新 / live-fetch」；仅当用户 **明确要求** 联网 legacy 时才用 `--live-fetch`。
4. **结构说明**：`markdown_summary` 已内建 **大盘与情绪 → 六大板块（每条含 `事件 / 影响 / 角度` 四行，对齐 legacy pipeline；高 importance 深度稿经 Router 补位进板块，默认无独立「📌 深度资讯」小节，`FINANCE_DB_SNAPSHOT_SHOW_DEEP_SECTION=1` 可开）→ 大事件 → 热点 → 社媒 → Top3**；JSON **`sections.deep_news`** 仍保留供检索。云路径约 **1～2 分钟**，须等命令结束。缺口以 `errors` 为准。
5. **旧版五段式**（大盘→焦点→新闻摘要→海外→选题）**仅**保留给：用户明确要求「写一份你手搓的盘面解读/复盘」、或对标账号分析/复盘技能等 **非 ingest 快照** 场景；与 §4.1 本条 **不**混用。

### 4.1A 可选：用户追问时的联网核对（Agent 层）

若用户**主动**要求核实北向、社媒、某板块等，且当前会话具备 WebSearch，可在**不修改**已发出的 `markdown_summary` 前提下，在后续消息中自行检索并说明来源；不得覆盖 JSON 里 API 给出的指数、涨跌幅、北向等数值。
