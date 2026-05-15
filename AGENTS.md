# AGENTS.md - 工作区元规则（Stream-Gen）

这是你的家。每次 session 开始前按这份规矩走，不用问我。

---

## First Run（仅在有 `BOOTSTRAP.md` 时）

若存在 `BOOTSTRAP.md`，按其中流程完成**首次**建档，把结果写入 `USER.md` / 必要时 `IDENTITY.md`，然后**删除 `BOOTSTRAP.md`**。

---

## Every Session（每次对话开头必读）

按此顺序读：

1. `IDENTITY.md` — 你是谁
2. `SOUL.md` — 服务方式与飞书/隐私边界
3. `USER.md` — 对面前协作者/客户的画像
4. `memory/YYYY-MM-DD.md`（今天 + 昨天，若存在）
5. `MEMORY.md` — 长期精华（**仅 1v1/私人语境**，见下）；内含「场景分流」，涉及行情快照或开稿时再 **`read`** `memory/rules/MEMORY_ingest.md` / `MEMORY_workflow.md`
6. `TOOLS.md` — 本环境、飞书与凭据说明

---

## 何时读 `MEMORY.md`（费控）

- **飞书 与机器人的单聊、或你与管理员的本地 CLI 1v1** — 可读 `MEMORY.md`。
- **群聊/多人可见会话** — **不读** `MEMORY.md`（防个人偏好泄漏给群内其他人）。

拿不准时 **优先不读**。

---

## Memory 机制

- **日流水**：`memory/YYYY-MM-DD.md` — 当天重要交互与决定。
- **长期精华**：`MEMORY.md` — 可复用的稳定偏好/约定；**不写**密文或完整 key。

客户明确「记住这个」时写入文件；**口头心记不跨 session**。

---

## 上下文与轮次（费 token · 与 MEMORY 同步）

- **少复述**：状态以磁盘与工具结果为准；勿在回复里反复粘贴整段快照/逐字稿/旧 tool 全文（详见 `MEMORY.md`「上下文预算与会话轮次」）。
- **控制长度**：同会话约 **12～15 个用户回合**或单主题已收尾时，**建议领航员新开飞书会话**再继续下一主题；长线程切换前可把交接写在 **当日** `memory/YYYY-MM-DD.md`（稿件 id、阶段、待办）。
- **MEMORY 碎片**：信源/开稿长规则已拆至 `memory/rules/`；默认只注入精简后的 `MEMORY.md`，按任务再读碎片，避免每轮堆全文。

---

## 安全

- 不越权访问其他 agent 的 workspace。
- 破坏性/批量外发操作需明确确认（见 `TOOLS.md`）。

技能（skills）说明「怎么做」；`TOOLS.md` 记录「你这套环境里飞书/凭据具体长什么样」。

---

## 行情 / 热点「只看数」（飞书常见问法）

用户只要**拉行情、拉热点、信源快照、今日全量、今日讯息**等（**单说其中一词也算**）且**不开稿**时：

- **默认（v0.3.0 云模式，`FINANCE_CLOUD_MODE=1`）**：`python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --summary-only`（HTTP 拉云端 pre-Router → 本地 Router/Rewriter；须 `.env` 配 `FINANCE_CLOUD_API_*`）。  
- **Advanced 本地 SQLite**：`.env` 设 `FINANCE_CLOUD_MODE=0` 后同上命令（读 `user_data/finance_sources.db`）。  
  从本地数据库读取最近 24h 数据，**不发起任何网络请求**；把 stdout JSON 里的 **`markdown_summary` 全文原样**发给用户。  
- **若 DB 为空 / 数据不新鲜**：`markdown_summary` 中会包含提示，**不要自动触发实时抓取**，告知用户"请等待定时采集（北京时间 09:00/14:00/20:00，cron 内 `TZ=Asia/Shanghai`）或手动更新"。
- **用户明确说"刷新/更新/重新拉取"时**：`python3 skills/streamy-content-gen/scripts/query_market_facts.py --live-fetch --sources market,news,social --summary-only`（实时联网，约 60–120s）。

禁止直接调用 `finance-source-ingest/scripts/ingest.py` 后结束，禁止拆成只跑 `fetch_market.py`、禁止按用户措辞只拉 `market` 或 `news`、禁止拆成两条消息各贴一半。

## 开稿阶段回复边界（新增）

- 进入带方向开稿后，`topic_picking` 默认只回：`#<DID>` + 候选（标题/核心论点/3条论据）+ 选号提示。
- 用户确认选题后、进入 `outline_refining` 前，必须先生成并展示该候选方向的 `evidence_pack`（`preflight_topic.py --candidate-id <N> --topic-payload-file ... --snapshot-path ...`），并用 `draft_manager update --set-evidence-pack-file` 落盘；再执行 user-style 门禁：列出 `user-style-manager` 中全部可用风格、让用户选择、并用 `draft_manager update --set-style-id` 绑定；未落盘证据包或 `style_id` 为空时不得生成大纲/逐字稿。
- `outline.md` / `script.md` / `meta.json` 不得手写或用 edit/write 直接改；大纲与逐字稿必须用 `draft_manager update --stage ... --payload-file ...` 落盘。若怀疑已有稿被绕过，先跑 `draft_manager.py doctor --draft <DID> --json`；归档稿加 `--include-archive`。
- 进入 `outline_refining` / `script_refining` 后，只回当前阶段产物与确认动作。
- 除非用户明确要求“回看数据/快讯来源”，否则不再拼接「信源状态/大盘行情/市场焦点/事实依据」块。

### 纯拉数（ingest 与包装脚本）

v0.2.2 起，**飞书端日常拉数走 DB 路径**（`query_market_facts.py` 默认不联网），数据由定时 cron 入库（北京时间 09:00/14:00/20:00，`setup_cron.sh` 写入 `TZ=Asia/Shanghai`）。`finance-source-ingest` 负责采集入库；`finance-draft-manager/scripts/db_snapshot.py` 负责从 DB 构建快照；`query_market_facts.py` 是对外统一入口。**不再**自动拼接 Tavily 或任何「联网补充」附录。若业务仍需要人工联网核对北向/社媒等，由 Agent 在对话中自行检索，且不得覆盖 DB 里的数值。

---

## Workspace 安装 / 跨机部署（执行方必读）

当用户要求「按仓库部署 / clone 本 workspace / 跑 download_and_install」时：

1. 在 **stream-gen 仓库根**运行 `./download_and_install.sh`。若含 **`[AGENT_INSTRUCTION_REQUIRED]`**：读 **`.env.example`**；**Router + Rewriter 默认开**；**P1 默认云**：`FINANCE_CLOUD_API_BASE_URL` + `FINANCE_CLOUD_API_KEY`（外网须安全组 8080）；**Advanced 本地** 才要 Tushare/RSSHub；飞书 **选配**。写 **`.env`** 时**禁止**编造密钥；总结**不要**复述完整密钥。  
2. 若输出含 **`[P1_GAP]`** 或 **`verify_env:`** 错误：置顶说明并要求补配；**禁止**在仍有缺口时宣称部署成功。若含 **`OPENCLAW_DIAG`**：按 JSON 与 **`DEPLOY.md`** 解释。  
3. **问财 zip**：整仓存在 `../scripts/iwencai_skillhub_download_and_install.sh` 时可用；勿与 **`download_and_install.sh`** 混淆。

---

## 心跳

`HEARTBEAT.md` 为空则跳过；需要定时轮询时再把任务写进去。
