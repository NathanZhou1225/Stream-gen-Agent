# streamy-content-gen v0.1 PRD

> **短直内容文本产出 Skill** · v1 范围封闭版
>
> - **状态**：Ready for Implementation
> - **版本**：v0.1.0-draft
> - **适用 Agent**：streamy（`/root/.openclaw/workspace-streamy/`）
> - **作者**：Nathan × streamy Agent
> - **最后更新**：2026-04-21

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [v1 范围](#2-v1-范围)
3. [使用者与触发](#3-使用者与触发)
4. [Draft 数据模型](#4-draft-数据模型)
5. [交互流（三段式）](#5-交互流三段式)
6. [目录结构](#6-目录结构)
7. [数据源规格](#7-数据源规格)
8. [生成执行模型](#8-生成执行模型方式-a)
9. [合规处理](#9-合规处理)
10. [产出物规格](#10-产出物规格)
11. [运行时契约](#11-运行时契约)
12. [可复用打包](#12-可复用打包)
13. [验收标准](#13-验收标准)
14. [延迟事项清单](#14-延迟事项清单)
15. [附录 A：关键决策回溯](#附录-a关键决策回溯)

---

## 1. 背景与目标

服务于券商 / 理财公司网销线的 **"领航员内容运营副驾驶"**。v1 聚焦**功能一**：短视频 / 直播的内容文本产出（框架 → 大纲 → 逐字稿）。

### v1 定位：骨架版

v1 不是追求内容质量的版本，而是搭建骨架：

- 跑通三段式 Draft 交互流水线
- 结构化落盘产出物，为 v2 人设学习 / 功能二复盘 喂数据
- 打通数据源接入管道

个性化（人设）和平台差异化（画像）均留给 v2。v1 产出的"通用财经短视频文案"本身与市面上任意 LLM 的产出差异不大 —— **v1 的价值在骨架，不在内容**。

---

## 2. v1 范围

### ✅ In Scope（v1 做）

- 三段式流水线（选题 → 大纲 → 逐字稿）
- 多 Draft 并行 + 短 ID（#A3F 形式）管理
- 自然语言交互（通过 openclaw 接入微信 / 飞书）
- Draft 状态持久化到 workspace 本地
- Tushare 市场数据接入
- 公开热榜聚合抓取
- 财联社电报抽象接口（stub 实现，留待配置凭据）
- 逐字稿末尾一次轻量合规扫描
- 产出物结构化落盘（6 个文件）
- Draft 生命周期管理命令（list / switch / show / drop / finalize）
- 可复用打包（shared-skills 软链 + 未来 clawhub 发布）

### ❌ Out of Scope（v1 不做）

- 领航员人设建模（v2）
- 平台画像差异化（v2）
- Fork 功能（基于旧稿生新稿，v2）
- 并行多风格生成
- 数据复盘（功能二，独立 skill）
- 深度合规监控（功能三，独立 skill）
- 对标账号拆解（功能四，独立 skill）
- 小红书图文（独立模型）
- Wind / iFinD / Choice / 内部研报系统
- 内部合规黑词库对接（词表留替换位）
- Draft 自动过期 / TTL（见第 14 节）

---

## 3. 使用者与触发

| 维度 | 规格 |
|---|---|
| 使用者 | 领航员 + 内容运营（协作场景） |
| 接入端 | openclaw 对接的微信 / 飞书（每个端口一个 user_id） |
| 触发词（示例） | "出稿 / 出逐字稿 / 出短视频脚本 / 开新 Draft / 出个口播稿" |
| 非触发场景 | 讨论代码、数据、合规、复盘时提到上述词汇 |
| 使用频率预期 | 单端口 ≈ 10 条 / 天 × 5 天 / 周 |

---

## 4. Draft 数据模型

### 4.1 生命周期状态机

```
  创建
   │
   ▼
┌─────────────────┐     用户"换一个方向"      ┌──────────┐
│ topic_picking   │ ◄─────────────────────── │ outline  │
│ 待选方向         │                          │ refining │
└────────┬────────┘   用户确认方向            └─────┬────┘
         │           ──────────────────────────────►│
         │                                          │
         │                                          │ 用户"出逐字稿"
         │                                          ▼
         │                                    ┌──────────┐
         │                                    │ script   │
         │                                    │ refining │
         │                                    └─────┬────┘
         │                                          │ 用户"定稿"
         │                                          ▼
         │                                    ┌──────────┐
         │                 用户"放弃"          │finalized │
         └──────────────────►  dropped ◄──────┤          │
                                              └──────────┘
```

状态枚举：`topic_picking` / `outline_refining` / `script_refining` / `finalized` / `dropped`

### 4.2 Draft ID 规则

- **格式**：3 位字母数字短 ID，形如 `A3F` / `B7K`
- **字符集**：大写字母 + 数字（排除易混淆的 `0/O/I/1`）
- **生成**：随机，碰撞时重试
- **作用域**：每个 `user_id` 独立命名空间
- **对话引用**：`#A3F` 形式

### 4.3 焦点 Draft 规则

- 用户指令默认作用于 **最近一次被提及 / 操作过的 Draft**
- 当焦点不明确时，Agent 主动反问 "你说的是 #A3F 还是 #B7K？"
- 用户可显式指定：`#B7K 大纲把论据 2 去掉`
- 焦点信息记录在 `drafts/index.json` → `users.<user_id>.focus`

### 4.4 多 Draft 并行规则

- 同一 user_id 可同时持有多条未定稿 Draft
- 开新 Draft 时，若已有 active Draft，Agent **主动提示** "当前有 #A3F 进行中，开新的还是继续？"
- 不限制最大并行数量（用户自行管理）

---

## 5. 交互流（三段式）

### 5.1 标准对话范本

```
👤 用户：帮我出一条抖音的短视频稿

🤖 Agent：好，开新 Draft #A3F。v1 还没做个性化，我按通用财经风格写。
        先聊"今天聊什么"——原材料任选其一：
        1) 直接告诉我主题
        2) 我拉今日大盘 + 公开热榜
        3) 你粘贴新闻 / 电报

👤 用户：2

🤖 Agent：（调 fetch_market.py + fetch_hot_rank.py）

        今日市场速览（2026-04-21 收盘）：
        • 上证 3428（+0.82%）深成 10921（+1.15%）
        • 主力净流入 +127 亿
        • 领涨板块：AI算力 / 半导体

        公开热榜 Top 10：
        1. 央行降准 0.25 个百分点
        2. 英伟达新品发布会
        ...

        你想聊哪个？

👤 用户：1，再加一点关于A股历史上降准后的表现

🤖 Agent：围绕"央行降准 + A股历史表现"，3 个方向：
        ① 【反直觉钩子】降准≠立刻涨，上次就跌了三天
        ② 【数据派】过去10次降准后30天 A 股涨跌中位数 +2.1%
        ③ 【情绪派】散户又在问"这次不一样吗"

        选 ①/②/③，或告诉我想怎么改。

👤 用户：② 不错，加一个"这次有什么不一样"

🤖 Agent：（推进到 outline_refining）

        ──── 大纲 #A3F ────
        [Hook · 3s]  过去10次降准A股平均30天涨2.1%——但数据骗了很多人
        [论据1]      10次降准后30天涨跌分布
        [论据2]      涨跌分化的根源
        [转折]       这次和过去三点不一样
        [行动启发]   该看哪几个指标
        [CTA]        加粉话术占位
        ──────────────

👤 用户：论据2去掉，转折改成两点最核心

🤖 Agent：（应用修改）已改。确认了说"出逐字稿"。

👤 用户：出逐字稿

🤖 Agent：（推进到 script_refining）

        ──── 逐字稿 #A3F ────
        [0:00-0:03 · Hook · 贴纸:惊叹表情]
        "过去10次降准，A股平均30天涨2.1%——但这个数据骗了很多人。"

        [0:03-0:15 · 论据1 · 配图:柱状图]
        ...

        [0:45-0:58 · CTA · 贴纸:箭头]
        "想拿今天这张降准复盘表，评论区扣'1'我私发。"
        ──────────────

        合规扫描：🟢 通过
        修改还是定稿？

👤 用户：定稿

🤖 Agent：已归档到 archive/2026-04-21/<user_id>/A3F/
        可以开新 Draft 了。
```

### 5.2 生命周期管理命令

随时可触发，不受当前阶段限制：

| 用户说 | Agent 动作 |
|---|---|
| "我手上有哪些稿" / "list" | 展示当前 user 下所有 active Draft（ID / stage / 创建时间） |
| "切到 B7K" / "继续 #B7K" | 切换焦点 Draft |
| "A3F 现在到哪了" / "show #A3F" | 展示指定 Draft 最新产物 |
| "放弃 #A3F" / "drop A3F" | 标记 dropped 归档 |
| "换一个方向" | 回退到 topic_picking 阶段（仅在 outline_refining 阶段允许） |

### 5.3 交互细则

- **开场不问平台**：v1 不做差异化，`platform` 字段留空
- **热点一次性展示**：市场数据 + 热榜合并展示，不做二级选择
- **分隔线**：使用 `──── 标题 ────` 格式（微信 / 飞书可渲染）
- **合规状态标记**：🟢 通过 / 🟡 有告警
- **推进靠自然语言**：用户说 "出逐字稿" / "定稿" / "换方向" 等，Agent 语义识别后触发脚本
- **每次人改** append 到 `history.json`（不做 diff，仅记录时间 + 修改描述 + 前后快照摘要）

---

## 6. 目录结构

### 6.1 Skill 仓库

```
streamy-content-gen/
├── SKILL.md                          ← 主入口，Agent 必读
├── _meta.json                        ← 版本号（clawhub 兼容）
├── README.md                         ← 面向人的说明（安装 / 环境变量 / 合规词表替换）
│
├── prompts/                          ← 给 Agent 本体读的 prompt 模板
│   ├── topic-generation.md
│   ├── outline-generation.md
│   ├── script-generation.md
│   └── natural-language-intent.md    ← 意图识别清单
│
├── templates/
│   ├── outline.md                    ← 大纲对话展示模板
│   ├── script.md                     ← 逐字稿对话展示模板
│   └── script.schema.json            ← 结构化产出 schema
│
├── references/
│   ├── interaction-flow.md           ← 三段式对话流规范
│   ├── draft-lifecycle.md            ← Draft 状态机详解
│   ├── compliance-lite.md            ← 轻量合规扫描规则
│   └── cta-patterns.md               ← 通用加粉话术库（占位版）
│
├── data/
│   └── compliance/
│       ├── blacklist-common.txt      ← 通用黑词（起步版占位）
│       └── README.md                 ← 提示替换为公司合规词表
│
└── scripts/
    ├── fetch_market.py               ← Tushare 市场数据
    ├── fetch_hot_rank.py             ← 公开热榜聚合
    ├── fetch_cls_telegram.py         ← 财联社电报 stub
    ├── draft_manager.py              ← Draft CRUD 核心
    └── lite_compliance_scan.py       ← 逐字稿扫描
```

### 6.2 Workspace 存储（在 `workspace-streamy/drafts/`，由 skill 读写）

```
drafts/
├── index.json                        ← 所有 user 的 Draft 索引
├── active/
│   └── {user_id}/
│       └── {draft_id}/
│           ├── meta.json             ← id, user_id, stage, 时间戳
│           ├── topic_candidates.json ← 阶段 1 产物
│           ├── outline.md            ← 阶段 2 产物（人看）
│           ├── outline.json          ← 阶段 2 产物（机器看）
│           ├── script.md             ← 阶段 3 产物（人看）
│           ├── script.json           ← 阶段 3 产物（机器看）
│           └── history.json          ← 所有人改记录（append-only）
└── archive/
    └── {YYYY-MM-DD}/
        └── {user_id}/
            └── {draft_id}/           ← 同上 6 个文件 + finalized_at
```

**`index.json` 结构**：

```json
{
  "users": {
    "nathan-wechat": {
      "active_drafts": ["A3F", "B7K"],
      "focus": "A3F",
      "last_activity": "2026-04-21T15:23:00+08:00"
    }
  },
  "version": 1
}
```

---

## 7. 数据源规格

| 数据源 | 实现 | 所需配置 | Fallback |
|---|---|---|---|
| **Tushare** | `fetch_market.py` 调 tushare SDK | 环境变量 `TUSHARE_TOKEN` | 无 token → 返回 `"error_type": "config_missing"`，用户改走 BYOD 路径 |
| **公开热榜聚合** | `fetch_hot_rank.py` 抓取公开聚合页 | 无 | 抓取失败 → 返回空数组 + 提示 |
| **财联社电报** | `fetch_cls_telegram.py` stub | 用户自行填入凭据（环境变量 `CLS_TELEGRAM_TOKEN`） | 默认 stub 返回空数组 |

**关键原则**：任何数据源失败，skill 都不能崩，降级到 BYOD（用户粘贴）路径。

**v1 明确不接入**：

- Wind / 同花顺 iFinD / 东方财富 Choice（付费 license，单公司专属）
- 国金时刻 / 内部研报系统（内部资源，跨公司不可复用）
- 华尔街见闻 / 选股宝（付费墙）
- 雪球 / 股吧 情绪分析（需上 NLP，杀鸡用牛刀）

---

## 8. 生成执行模型

- skill **零 LLM 调用**
- 所有"写字"由 openclaw Agent 本体（Claude / Composer）执行
- skill 提供给 Agent 的是：
  - `prompts/*.md` 里的**生成指令模板**
  - `templates/*.md` 里的**输出格式模板**
  - 脚本拉回来的**数据 context**
- Agent 读完 prompt + 数据 + 对话历史 → 自己 produce 文本 → 调 `draft_manager.py update` 落盘

**好处**：

- 省 token（skill 不重复定义生成 prompt）
- 换模型零成本（不绑定特定 LLM 的 API）
- 和仓库内其他 skill 风格一致（`ai-stock-picker` 等）

---

## 9. 合规处理

| 层级 | 规则 |
|---|---|
| **时机** | 仅在逐字稿生成后扫描一次（不在选题 / 大纲阶段扫） |
| **词表位置** | `data/compliance/blacklist-common.txt` |
| **v1 默认词表** | 占位版，含 "必涨 / 稳赚 / 保证收益 / 荐股 / 一定涨 / 包赚" 等明显违规词 |
| **命中行为** | **只告警不改写**。展示命中词 + 所在时间点，例："🟡 命中 2 个：'保证收益'（0:35）、'必涨'（0:48）" |
| **用户决策权** | 用户自行决定改或不改，skill 不自动重写 |
| **公司定制** | `data/compliance/README.md` 明确提示替换为贵司合规黑词库 |

---

## 10. 产出物规格

### 10.1 每条定稿 Draft 归档 6 个文件

| 文件 | 用途 | 消费方 |
|---|---|---|
| `script.md` | 逐字稿（带时间轴、视觉标注） | 人（领航员录制） |
| `script.json` | 逐字稿结构化版 | 功能二复盘 skill |
| `outline.md` | 大纲（人看） | 人、v2 人设学习 |
| `outline.json` | 大纲结构化 | 功能二 |
| `topic_candidates.json` | 选题候选 + 最终选择 + 修改轨迹 | v2 人设学习 |
| `history.json` | 所有人改痕迹（append-only） | v2 人设学习 + 功能二 |

### 10.2 `script.json` Schema

```json
{
  "draft_id": "A3F",
  "user_id": "nathan-wechat",
  "finalized_at": "2026-04-21T15:30:00+08:00",
  "title": "降准不等于立刻涨",
  "duration_sec": 58,
  "segments": [
    {
      "time": "0:00-0:03",
      "role": "hook",
      "say": "...",
      "visual": ["贴纸:惊叹表情"]
    }
  ],
  "cta": {
    "type": "add_wechat",
    "position": "ending",
    "phrasing": "想拿今天这张降准复盘表，评论区扣'1'我私发"
  },
  "compliance": {
    "status": "pass",
    "warnings": []
  },
  "source": {
    "topic": "央行降准+A股历史表现",
    "data_sources": ["tushare:index_daily", "hot_rank:weibo"]
  }
}
```

### 10.3 CTA 话术模板（v1 占位版，共 3 条）

> 业务方定稿话术后替换 `references/cta-patterns.md`。

1. **资料钩子**：`"想拿今天这张 {topic} 表，评论区扣 1 我私发"`
2. **社群引导**：`"想看更多这类分析，关注后进我的财经交流群"`
3. **私信指令**：`"对 {topic} 感兴趣的朋友，私信我回复 '{keyword}' 即可领取"`

---

## 11. 运行时契约

> **重要**：下列环境变量是 skill 与 openclaw 运行时之间的**约定**。当前 openclaw 运行时（`openclaw-weixin` / 飞书插件）**尚未自动注入这些变量**，需要后续在网关层打通。v1 skill 提供 fallback，不阻塞启动。

| 环境变量 | 来源 | 用途 | Fallback |
|---|---|---|---|
| `OPENCLAW_USER_ID` | openclaw 网关在调用 skill 脚本前注入，值为当前消息的 `senderId` / `open_id` | Draft 归属 | 未设置 → 使用 `"default"` |
| `OPENCLAW_ENDPOINT_ID` | 同上，标识消息来源端口（wechat / feishu / accountId） | 多端口隔离 | 未设置 → 使用 `"default"` |
| `OPENCLAW_WORKSPACE` | openclaw 启动 Agent 时的 workspace 路径 | Draft 文件存储根目录 | 未设置 → 使用 `$(pwd)` |
| `TUSHARE_TOKEN` | 用户配置（`.env`） | 市场数据接入 | 未设置 → BYOD 路径 |
| `CLS_TELEGRAM_TOKEN` | 用户配置（`.env`） | 财联社电报接入 | 未设置 → stub 返回空 |

**对接指引**（给后续打通网关层的同学）：

- 在 `openclaw-weixin` / `feishu-openclaw-plugin` 调用 skill 脚本时，将 `ctx.senderId` 注入 `OPENCLAW_USER_ID`
- 在 `accountId` 基础上生成 `OPENCLAW_ENDPOINT_ID`（如 `feishu:streamy`）
- 12-Factor 原则：ID 走环境变量，不经 LLM，不做模型侧权限管理

---

## 12. 可复用打包

| 阶段 | 方式 |
|---|---|
| **v1** | 落在 `/root/.openclaw/shared-skills/streamy-content-gen/`，各 workspace 用软链接复用 |
| **v2** | ClawdHub 发布（`_meta.json.ownerId` 置真实 ID） |

### 打包前置条件

- `_meta.json` 填版本号，`ownerId: "placeholder"`（v2 发布时替换）
- `README.md` 写清：
  - 安装步骤（软链接到 `workspace-*/skills/`）
  - 环境变量配置（见第 11 节）
  - 合规词表替换步骤（见第 9 节）
- 脚本 0 硬编码路径（全部用 `os.environ['OPENCLAW_WORKSPACE']` 或相对 skill 根）
- 所有文本使用 UTF-8，脚本使用 Python 3.9+

### `_meta.json` 模板

```json
{
  "ownerId": "placeholder",
  "slug": "streamy-content-gen",
  "version": "0.1.0",
  "publishedAt": 0
}
```

---

## 13. 验收标准

v1 算完成，必须满足以下 7 条：

1. **单 Draft 端到端**：在 openclaw 里说 "出一条抖音稿"，能走完三段产出定稿，归档 6 个文件
2. **多 Draft 并行**：开 #A3F，中途开 #B7K，再回 #A3F 继续改，两条互不干扰
3. **数据源降级**：删掉 `TUSHARE_TOKEN` 环境变量，skill 不崩，走 BYOD 路径仍能产出稿
4. **合规扫描**：在大纲里塞进一句 "这股必涨"，定稿前能扫出来告警
5. **history 完整性**：一条 Draft 里改 3 次大纲、2 次逐字稿，`history.json` 里有 5 条变更记录，每条带 timestamp 和 修改描述
6. **跨 workspace 复用**：把 skill 软链到一个空白的新 workspace，能照样跑通
7. **SKILL.md 可读性**：一个从没看过这个 skill 的 Agent，读完 `SKILL.md` + 一个 `prompts/*.md` 就能上手

---

## 14. 延迟事项清单

| 项 | 延迟到 | 原因 |
|---|---|---|
| 领航员人设 yaml 建模 | v2 | 先验证流水线 |
| 抖音 / 视频号 / 快手差异化画像 | v2 | 同上 |
| Fork Draft（基于旧稿起新稿） | v2 | 节省 v1 范围 |
| 并行多风格生成 | 不做 | 违反三段式交互 |
| **Draft 自动过期 / TTL** | **v2（明确延迟）** | **v1 用户手动 drop，v2 补自动归档策略（建议 X 天未更新 → dropped）** |
| 财联社电报实接 | 看凭据 | v1 留抽象接口，用户配置凭据后激活 |
| 合规词表业务化 | 部署时 | README 指明替换位 |
| ClawdHub 发布 | v2 | 先本地跑通 |
| openclaw 网关层 user_id 注入 | 后续 | skill 已定契约（见第 11 节），v1 用 `"default"` fallback |

---

## 附录 A：关键决策回溯

| 决策项 | 选择 | 原因 |
|---|---|---|
| 使用者 | 场景 C（领航员 + 运营协作） | 两边都要用 |
| 流水线切分 | 三段式（选题 / 大纲 / 逐字稿） | 选题与大纲解耦，不繁琐 |
| 人改形态 | 纯自然语言 | 微信 / 飞书端降低门槛 |
| Draft 并发 | 多单并行 + 短 ID | 10 条 / 天节奏下避免憋单 |
| 状态存储 | workspace 本地文件 | 可复用、零依赖 |
| v1 人设 | 不做 | 先搭骨架 |
| v1 平台差异 | 不做 | 同上 |
| 数据源 | Tushare + 热榜 + 财联社 stub | 避开闭源付费与内部资源 |
| 生成执行 | Agent 本体，skill 零 LLM 调用 | 省 token、换模型零成本 |
| 合规扫描 | 仅逐字稿末尾一次，只告警不改写 | 省时，保留用户决定权 |
| user_id 传递 | 环境变量 `OPENCLAW_USER_ID` | 12-Factor，LLM 侧不做权限管理 |
| ownerId | `"placeholder"` | v2 发 ClawdHub 时替换 |
| TTL | v1 不做，v2 补 | 先最小可用 |

---

**文档状态**：冻结，待实施。
