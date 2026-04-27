# finance-source-ingest

**独立**金融/社媒等信源聚合 skill：单进程 stdout 输出**一个** JSON、不调 LLM、**不写** `drafts/`。与 `streamy-content-gen` 的 Draft 流程解耦。

- **权威入口**：[./SKILL.md](./SKILL.md)（能力、铁律、与 `streamy` 的边界）  
- **可执行体**：`scripts/ingest.py`（`run` 子命令等）  
- **Python 依赖**：见 [./requirements.txt](./requirements.txt)（`akshare`，`feedparser`）

## 新机器 / 换目录 / 迁移 OpenClaw（必读）

Python 的 **`.venv` 会在 `bin/pip` 等脚本里写入创建时的绝对路径**。若 **从另一台机器拷贝了旧 `.venv`**，易指向旧绝对路径，应**删掉**后重建。

**不要**把 `.venv` 打进 git、不要拷 venv 迁机（本仓库 [./.gitignore](./.gitignore) 已忽略 `.venv/`）。

### 方式 A：自动（推荐）

在 skill **根目录**用系统 `python3` 直接跑 **`scripts/ingest.py`** 即可：若本地尚无 `.venv`，**首次**会自动 `python3 -m venv`、再 `pip install -r requirements.txt`，并**自动切换**到 `.venv` 内解释器再执行（实现见 `scripts/_venv_bootstrap.py`）。**需出网**完成 pip 安装。

- 关闭自动建环境（例如只读/离线检查）：`export FINANCE_INGEST_NO_AUTO_VENV=1` 后照常调用 `ingest.py`。
- 迁机时：**不要**打包旧 **`.venv`**；只拷 `finance-source-ingest` 代码树，**第一次**在目标机跑任意 `ingest run` 即会自举；若你主动拷了坏 venv，删掉 `rm -rf .venv` 再跑一次即可。

### 方式 B：显式脚本

进入本 skill 根目录执行 [./scripts/bootstrap_venv.sh](./scripts/bootstrap_venv.sh)（`BOOTSTRAP_FORCE=1` 可删后重建），与上一种效果一致，仅多一步手敲。

3. 日常运行优先 **`.venv/bin/python scripts/ingest.py`** 或让 **方式 A** 自举；`preflight_topic.py` 会优先用兄弟目录下 `.venv/bin/python`（无则系统 `python3`，会触发方式 A 自举直至 `.venv` 存在）。

升级包：`.venv/bin/python -m pip install -U -r requirements.txt`（推荐用 `python -m pip`）。

## 推荐用法（摘录）

在含本目录的 workspace 中 **先 `bootstrap_venv.sh`**，再用 venv 里的解释器：

```bash
# 示例；具体 `run` 参数以 SKILL 与 `ingest.py --help` 为准
.venv/bin/python scripts/ingest.py run --sources market,news,social
```

`stdout` 为单个 JSON 对象，含 `sections` / `errors` / `markdown_summary` 等，详见 SKILL。

## 与 streamy-content-gen

`preflight_topic.py` 通过 **subprocess** 调用本目录下的 `ingest`（可 `--finance-root` 指根路径）。不在本 skill 内 `import` streamy 或写 Draft。

## 产品文档

- 与 streamy 共用 workspace 的 PRD 时，应出现在  
  `workspace-streamy/docs/PRD-finance-source-ingest-v0.1.md`（若缺省，为占位/备份待补，见文内说明）。
