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

## 心跳

`HEARTBEAT.md` 为空则跳过；需要定时轮询时再把任务写进去。
