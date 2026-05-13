# 自然语言意图识别（slim index）

> Agent 收到用户消息后的**第一步**：把口语映射到**具体命令或阶段动作**。一切与 `SKILL.md` 冲突时以 **SKILL.md** 为准。

## 默认必读

1. **`prompts/fragments/intent-core.md`**  
   意图总纲、生命周期命令、阶段推进硬边界、`query_market_facts` 全量契约、复合意图优先级、焦点消歧、反例与兜底。

2. **`SKILL.md`**（Playbook / 铁律 / 三段式顺序）

## 按需读取

- **`prompts/fragments/intent-examples.md`**  
  瘦身前**完整长版**：用户话术长表、阶段子表、archive 细则、更多反例与话术模板。遇到歧义、消歧、或 intent-core 表不够用再打开。

## 与逐字稿 / 大纲 prompt 的衔接

- `outline_refining`：读 `prompts/outline-generation.md`（索引 → core + min-schema）。
- `script_refining`：读 `prompts/script-generation.md`（索引 → core + min-schema）。
- 纯拉数、**未**进入带方向开稿链：只走 **`intent-core.md` §4.4**（`query_market_facts.py --sources market,news,social --max-items 30 --summary-only`），**不要**激活整段开稿流水线。

## 版本说明

- 路由与硬边界以 **`intent-core.md`** 与工具返回码为准；`intent-examples.md` 文首保留 v0.1.5 级变更速览供深度查阅。
