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
5. `MEMORY.md` — 长期精华（**仅 1v1/私人语境**，见下）
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

## 安全

- 不越权访问其他 agent 的 workspace。
- 破坏性/批量外发操作需明确确认（见 `TOOLS.md`）。

技能（skills）说明「怎么做」；`TOOLS.md` 记录「你这套环境里飞书/凭据具体长什么样」。

---

## 行情 / 热点「只看数」（飞书常见问法）

用户只要**拉行情、拉热点、信源快照、今日全量**等（**单说其中一词也算**）且**不开稿**时：见 **`skills/streamy-content-gen/prompts/natural-language-intent.md` §4.4** — 必须 **`python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --max-items 30`**，并把 stdout JSON 里的 **`markdown_summary` 全文原样**发给用户；禁止直接调用 `finance-source-ingest/scripts/ingest.py` 后结束，禁止拆成只跑 `fetch_market.py`、禁止按用户措辞只拉 `market` 或 `news`、禁止拆成两条消息各贴一半。

## 开稿阶段回复边界（新增）

- 进入带方向开稿后，`topic_picking` 默认只回：`#<DID>` + 候选（标题/核心论点/3条论据）+ 选号提示。
- 进入 `outline_refining` / `script_refining` 后，只回当前阶段产物与确认动作。
- 除非用户明确要求“回看数据/快讯来源”，否则不再拼接「信源状态/大盘行情/市场焦点/事实依据」块。

### WebSearch 兜底（Agent 层，不属于 ingest）

`finance-source-ingest` 只产出可迁移的 API 快照；用户侧纯拉数一律通过 `query_market_facts.py` 包装脚本执行。该脚本会在 JSON 顶层 `meta.websearch_required: true` 或 `meta.websearch_gaps` 非空时直接调用 Tavily，并在 **`markdown_summary` 原文之后**追加一段 **「联网补充（Tavily 兜底）」**。触发条件：

- 六大板块中任一板块只有「行情侧补充 / 暂无」而无财联社正文；
- 百度热榜或泛财经条目为空、无可用财经 `detail`，或 `meta.websearch_gaps` 标出「泛财经热点」；
- 社媒/人气榜/舆情为空，或相关接口失败；
- 北向资金为空、返回 `0` 或被 `meta.websearch_gaps` 标出异常时，只补充可核验口径/背景，不用搜索结果覆盖 API 数字；
- 「大事件」未命中国家/全球/政策/地缘/峰会类事件时，搜索近 3-7 天会影响金融市场的国家性/世界性事件；
- `markdown_summary` 的告警已中文化；若因 API 失败触发 WebSearch，附录中必须写明「因 XX 接口失败/为空，使用 WebSearch 兜底」。

硬规则：

- WebSearch 结果**不得改写、删减、插入** `markdown_summary` 原文，只能追加独立附录；
- WebSearch 能力可来自平台原生 WebSearch、Brave API，或当前 workspace 已安装且可执行的搜索 skill；不要把 Brave API 当成唯一联网方式；
- 每条补充必须标注 **板块/归属 + 来源标题或域名 + 时间**（无网页时间则写「检索时间」）；
- 每个缺口板块最多 1 次搜索、1-2 条补充；全量补充最多 6-8 条；
- 搜不到可靠来源时写「未找到可核验补充」，不得脑补；
- 禁止只复述 `markdown_summary` 里的「已触发 / 需 WebSearch」提示然后结束；必须给出 WebSearch 结果，或明确写「WebSearch 未执行成功：原因」；
- 若当前会话没有 WebSearch 工具或调用失败，仍要追加「联网补充」段，说明未能执行，不得把“建议 WebSearch”当作完成；
- 迁移本 workspace 时，若希望保留此能力，需连同本 `AGENTS.md`、`MEMORY.md` 与 `natural-language-intent.md` 的 Agent 层协议一起迁移。

---

## 心跳

`HEARTBEAT.md` 为空则跳过；需要定时轮询时再把任务写进去。
