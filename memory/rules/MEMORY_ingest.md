# MEMORY 碎片 · 信源快照（按需读取）

> 触发：用户要**今日行情、热点、快讯、信源快照、全量信息**且未进入手写复盘；或执行 `query_market_facts` / `ingest` 相关。**读完回到主任务，勿一次性混入开稿长流程（开稿规则见 `MEMORY_workflow.md`）。**
> 信源成败、失败原因、盲区、火山引擎部署适配性与 OpenClaw 契合度详见 `MEMORY_ingest_source.md`。

---

## 4.1 行情 / 热点 / 信源快照（与 `finance-source-ingest` 对齐）

当用户要**今日行情、今日热点、快讯、信源快照、全量信息**等（**未**进入带方向开稿链、**未**要求你手写复盘报告）时：

1. **唯一信源形态（全量口径）**：在 workspace 下执行 **`python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --summary-only`**（默认 **cache-first**：比对云端 `db_last_ingested_at`（`/api/v1/stats`），未变则读 **`cache/snapshot/snapshot.json`**；stale 或 `--force-refresh` 才云拉 + 本机 Router/Rewriter）。**禁止**再跑 `ingest.py run` 或本地 `finance_sources.db`；**不**自动 Tavily。OpenClaw **`exec` 的 `timeout` 须 ≥ 300**（秒）；cache 命中通常 **<1s**，冷拉约 **90–120 秒**。  
   **快照缓存（开稿提速 · 缓存优先）**：成功冷拉时写入 **`workspace-stream-gen/cache/snapshot/snapshot.json`**（完整 JSON）。与 `preflight_topic.py` / `query_direction_brief.py` 共用；**同一 workspace 目录**下所有会话复用。**定时预热**：业务机 `scripts/setup_snapshot_warm_cron.sh`（**08:15 / 09:45 / 14:05 / 20:05** CST）。**迁移/新路径**：cron **不随文件夹走**，须在新根 `--install`（见 `docs/DEPLOY_WORKBUDDY.md` §3.1）。**热榜**：默认 `PREFLIGHT_SKIP_HOT_RANK=1`。
2. **展示契约（全量）**：用户明确要求「今日行情 / 热点 / 全量快照 / 全量信息」时，将 stdout JSON 的 **`markdown_summary` 全文原样**发出（可加 `meta.snapshot_cached` 一行说明是否读缓存）。**禁止**只贴三大指数或拆两条消息。
3. **带方向问信息（非全量 · 硬分流）**：用户已给方向且问「有什么相关信息 / 能否开稿 / 素材」但**未**要求全量表 → **`query_direction_brief.py --direction '…'`**，展示 `direction_brief.markdown_brief`；**禁止**为此目的贴全量 `markdown_summary`。若用户确认开稿 → `preflight_topic.py --direction`。
4. **禁止**主动提议「实时刷新 / live-fetch」；仅当用户 **明确要求** 联网 legacy 时才用 `--live-fetch` 或 QMF `--force-refresh`。

### 4.1A 可选：用户追问时的联网核对（Agent 层）

若用户**主动**要求核实北向、社媒、某板块等，且当前会话具备 WebSearch，可在**不修改**已发出的 `markdown_summary` 前提下，在后续消息中自行检索并说明来源；不得覆盖 JSON 里 API 给出的指数、涨跌幅、北向等数值。
