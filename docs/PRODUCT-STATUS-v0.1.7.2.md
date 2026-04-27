# 领航员内容副驾驶 · 产品阶段总结（**v0.1.7.2**）

> **文档作用**：在 PRD 之外，用一页说清**当前做对了什么、还没做什么**，覆盖 **`streamy-content-gen`** 与 **`finance-source-ingest`** 两条交付线，并交代飞书等集成现状。  
> **版本命名**：**文件名与 `streamy-content-gen` 的 `_meta.json` 对齐**，便于与历史 `PRODUCT-STATUS-*.md` 并存。  
> **读者**：产品、接手的工程、未来的自己。  
> **最后更新**：2026-04-25  
> **对 PRD 的关系**：`PRD-streamy-content-gen-v0.1.md`（功能①）与 `PRD-finance-source-ingest-v0.1.md`（信源）仍是**规格**；**本文件是阶段快照，随发布迭代。**

| 子系统 | 当前版本（本仓库 / `_meta.json`） | 根路径 |
|--------|----------------------------------|--------|
| `streamy-content-gen` | **0.1.7.2** | `workspace-streamy/streamy-content-gen/` |
| `finance-source-ingest` | **0.1.2** | `workspace-streamy/finance-source-ingest/` |

---

## 1. 总览：大产品在什么阶段

- **大产品**「领航员内容运营副驾驶」共规划 **4 条功能线**：①内容产出 ②内容复盘 ③异常监控 ④对标账号。  
- **本 workspace 以功能 ① 为重心**：短视频 / 直播**文本**（选题 → 大纲 → 逐字稿 / 分镜），经 **streamy** agent 接入 **飞书/微信**（以飞书实跑为主）。  
- ②③④ 不在本仓交付范围内，**本文件只在 §6 作路线图占位**。

---

## 2. 已经做好什么（`streamy-content-gen`）

### 2.1 核心流水线（产品价值）

- **Draft 全生命周期**由 `draft_manager.py` 落地：`create` / `list` / `show` / `switch` / `update` / `finalize` / `drop`，多稿并行、短 ID、归档、回退换方向、合规扫描写回。  
- **三阶段可落盘**：`topic_picking` → `outline_refining` → `script_refining`，**工具层禁止** forward 跳阶段、**禁止**无数据源的脑补候选、**禁止**不存在的 `draft_id` 变更。  
- **Thick 提示词**：`prompts/topic-generation.md` / `outline-*.md` / `script-generation` 等 + `natural-language-intent.md` 与 **`SKILL.md` Playbook** 对齐。  
- **合规**：`lite_compliance_scan.py` 内嵌在 `script_refining` 的 `update` 中；命中**只告警、不自动改稿**（与 PRD 一致）。  
- **内嵌热数据**：`fetch_market.py`（Tushare+新浪等）、`fetch_hot_rank.py`（tophub 主 + 备源）；财联社在 content-gen 侧仍有 **stub** 类脚本，**主路快讯**在 **`finance-source-ingest`**（见下）。

### 2.2 与飞书/Agent 的「软工程」

- **工具硬约束**已下沉到 Python；**L1 文档**在 `SKILL.md` 顶部 preflight、**L2** 在 `draft_manager` 返回的 `invariants[]`。  
- 针对 **ark-code 行为**，陆续增加 **`SOUL.md` / `AGENTS.md` 顶栏**、**`feishu-openclaw-plugin` 的 `feishu-channel-rules`（always-on）** 条，解决：  
  - 首问「口播/切入」就**整段分镜定稿**、跳过选题/大纲；  
  - 只发「今日热点/快讯」**贴完表就断线**、没有「下一步」开稿/三选；  
  - 为塞 CTA **压缩快讯条数**的模型倾向（`SKILL` / `SOUL` 与 **0.1.7.1** 明确禁止）。  
- **不能靠网关物理拦截** `write`/`edit` 改 drafts：OpenClaw 生命周期 hook **不含** per-tool 拦截，只能靠文档 + 工具撞墙 + 飞书条。  
- **v0.1.7.2+**：在飞书回显**定稿/整段逐字稿**时，约定**不要**用 Markdown 三反引号把全文包成**代码块**（易呈现为等宽+行号），改为**正文 + `####` + 加粗**；`feishu-channel-rules` always-on 与 `SKILL` 铁律 8 已写。

### 2.3 高确定性数据管道（与 ingest 的衔接）

- 推荐路径：**`ingest.py run | adapter_ingest_to_fact_snapshot.py | build_topic_payload.py` → `draft_manager update --stage topic_picking --payload-file`**，减少 LLM 手拼 JSON。  
- 详见 **`streamy-content-gen/README.md`** 与 **`SKILL.md` §2.1.1**。

---

## 3. 已经做好什么（`finance-source-ingest`）

- **独立 skill**（`ingest` **不调 LLM、不写 `drafts/`**）：`ingest.py run` 出 **单 JSON** + `markdown_summary` + `errors` + `invariants`。  
- **梯队一 行情**：AkShare 主（指数/北向/行业 Top5）+ 指数**短超时**与**新浪降级**等（见 `README` 与 PRD）。  
- **梯队二 快讯（财联社，AkShare）**：`stock_telegraph_cls` / `stock_info_global_cls` 等，关键词 OR 过滤、无命中时**回退最新 3 条** + `NEWS_KEYWORD_FALLBACK`；**单轮条上沿 20 条**（`max(1, min(max_items, 20))`，v0.1.2+），与 `markdown_summary` 中 news 展开展示上沿一致。原 RSS 主路已停，**PRD 附录**保留对照。  
- **梯队三 社媒**：第三方 JSON 或**内置降级**（微博+热榜等），无外网时可为空、不拖垮 `ok`。  
- **下游消费**：由 `streamy-content-gen` 编排，**不**在 ingest 里自动 `create` Draft。  
- **飞书/会话**：`skills/` 软链、网关/会话的 `skillsSnapshot` 与 content-gen 同类问题，见各自 `README`。

---

## 4. 还没做 / 已知风险（实话说）

### 4.1 `streamy-content-gen` 与集成

- **多通道「硬保证」**仍**没有**工具前 hook：若模型执意 `edit` 改 `script.json` / `topic_candidates.json`，**只能靠事后撞墙**与飞书/文档。  
- **`user_id` / 网关向 workspace 注 env**：协议在 PRD 里，**实机是否**向 `OPENCLAW_USER_ID` 等**自动注入**依赖部署；缺省多为 `default`。  
- **人设 / 多平台不同脚本 / Fork Draft / TTL**：PRD 已标 v2+。  
- **财联社「完整电报」在 content-gen 的单独脚本**：v1 仍以 stub/备用姿态存在；**主用 ingest 财联社**。

### 4.2 `finance-source-ingest`

- **AkShare/东财接口**若变更，需升级依赖或修适配。  
- **自爬社媒**仍为 **stub**；**双源对账行情** 未做。  
- **串联**不内嵌在 ingest 内，由 **content-gen 侧** shell/脚本维护；UAT/运维需**同一套 venv 与 `OPENCLAW_WORKSPACE`**。  
- 部署机 **DNS/出网** 会导致社媒等块**空**，在 `errors` 中可见，**非ingest 逻辑错误**。

### 4.3 功能 ②③④

- ② 内容复盘、③ 异常监控、④ 对标：**未在本仓按产品规格交付**；**scope 以业务方后续 PRD 为准**。

---

## 5. 验收与回归（便于审计）

- **飞书多轮验收**：`MEMORY.md` / 本仓 `README` 中记录 v0.1.2–v0.1.4 多路径、FactSnapshot 管道等；**0.1.5–0.1.7.x** 以**流程/飞书**行为与文档对拍为主。  
- **产品阶段判断**：**功能 ① 已具备**「可落盘、可复测、可飞书用」的 **v0.1** 形态；**0.1.7.1 不代表功能② 已开工**。

---

## 6. 相关文档与历史快照

| 文档 | 说明 |
|------|------|
| [PRD-streamy-content-gen-v0.1.md](./PRD-streamy-content-gen-v0.1.md) | 功能 ① 需求与契约（已冻结为 v0.1 基线） |
| [PRD-finance-source-ingest-v0.1.md](./PRD-finance-source-ingest-v0.1.md) | 信源技能 PRD（v0.1.2+ 与代码对齐处见文内 新闻条数 等） |
| [PRODUCT-STATUS-v0.1.4.md](./PRODUCT-STATUS-v0.1.4.md) | **2026-04-22** 前写的阶段全景；更细的里程碑表仍可参考 |
| [PRODUCT-STATUS-v0.1.7.1.md](./PRODUCT-STATUS-v0.1.7.1.md) | 0.1.7.1 代快照，见 §7 |
| 各子目录 `README.md`、`_meta.json` | 发行说明与开发进度以子项目为准 |

**当前阶段总结**以本文件（**PRODUCT-STATUS-v0.1.7.2.md**）为准；[PRODUCT-STATUS.md](./PRODUCT-STATUS.md) 为**入口**。

---

## 7. 修订记录（仅作索引）

| 日期 | 说明 |
|------|------|
| 2026-04-25 | 首版以 `PRODUCT-STATUS.md` 落盘，后**更名为** `PRODUCT-STATUS-v0.1.7.1.md`（与 `streamy-content-gen` 0.1.7.1 对齐）；内容对齐 `finance-source-ingest` 0.1.2 与 PRD 中 news 条数 10→20 的表述 |
| 2026-04-25 | **v0.1.7.2**：`streamy-content-gen` 升至 0.1.7.2；**飞书定稿/逐字稿** 禁止用三反引号包全文的**展示约定**（`feishu-channel-rules` + 铁律 8 + `AGENTS` L0''' + `script-generation` §12） |

当 **`streamy-content-gen` 版本**再次 bump 时，建议**复制**本文件为 `PRODUCT-STATUS-vX.Y.Z.md`、改头表与 §7、并把 [PRODUCT-STATUS.md](./PRODUCT-STATUS.md) 的链接指到新文件。
