# ADR-0008: 行为参数外部可配置化（config.toml）

- 状态：已接受
- 日期：2026-07-19

## 背景

智能体的所有"停止条件"原本散落两处：

- `agent/config.py` 里 9 个常量（`MAX_*`、`CRITIC_PASS_SCORE`、`UPSTREAM_BRIEF_LEN` 等）——
  **改它们要编辑源码**。
- 5 个模块里**硬编码字面量**（`llm.py` 的 `retries=3`/`timeout=120`/`max_tokens`、`tools.py`
  的 `timeout=30`/`[:500]`/`freshness`、`execute_reflect.py` 的 `result[:2000]`/`report[:4000]`、
  报告 `max_tokens=8192`、`plan_execute.py` 的 `result[:300]`）——**用户根本看不到、也改不了**。

目标：把这些**全部抽到外部配置**，部署者/用户不碰源码即可调。

## 决策

### 机制：`config.toml`（stdlib `tomllib`）

- **`config.example.toml`**（入库）：与内置默认一致 + 分组注释，是单一真相源 / 用户复制模板。
- **`config.toml`**（gitignore，用户本地）：覆盖默认。
- **`agent/config.py`**：内置默认作为兜底；`_load_overrides()` 读 `config.toml`（项目根），
  `_o_int/_o_float/_o_str` 按键取值，缺失/非法回退默认并告警。

加载顺序：`config.toml`（若存在）→ 否则空；每个值 `toml 覆盖 or 内置默认`。
**无 `config.toml` 时行为与历史完全一致**（零回归，已由 T1 单测锁定）。

### 范围：核心循环停止条件 + 生成/上下文上限（~18 值）

| 分组 | 键 |
|---|---|
| `[limits]` | `react_task_rounds` `react_rounds` `plan_steps` `dag_nodes` `reflect_retries` `report_retries` |
| `[review]` | `critic_pass_score` `critic_result_chars` `report_review_chars` |
| `[llm]` | `retries` `timeout` `max_tokens` `report_max_tokens` |
| `[search]` | `max_results` `timeout` `content_chars` `freshness` `upstream_brief_chars` |

### 密钥 vs 行为 分离

- `.env`（[[0003-deferred-candidates]] 既有）：密钥 + 模型 id。
- `config.toml`：行为参数。

两者职责清晰分开——调"重试几次"不必打开密钥文件。

## 为什么是 toml 而非 .env / json / yaml

| 方案 | 否决/采纳理由 |
|---|---|
| **toml（采纳）** | stdlib `tomllib`（3.11+）、可分组、**支持注释**、与密钥分离 |
| `.env` 覆盖 | 零依赖、与 MODEL 一致，但数值全成字符串（要 `int()` 转换 + 类型丢失）、与密钥混在一起 |
| `config.json` | 零依赖、3.10 兼容，但 **JSON 无注释**——调参体验差 |
| yaml | 体验好，但**引入 `pyyaml` 依赖**，违反零依赖原则 |

## Python 版本要求

`tomllib` 是 **Python 3.11 stdlib**。README 环境要求从 3.10+ 提到 **3.11+**（用户已在 3.11）。

## 未采纳项（防重提）

- **import 期常量**（`TODAY = ...` 模式用于日期，见 [[0007-current-date-injection]]）：配置要
  随部署变，不该绑死在源码字面量里；采纳的是外部 toml 覆盖。
- **`.env` 覆盖**：数值类型处理差、与密钥混杂，否决（理由见上表）。
- **yaml**：依赖，否决。
- **把 `MODEL` 也搬到 toml**：MODEL 已在 `.env` 可用，搬动是纯 churn；保持现状。

## 顺带修复

`plan_execute.py` 线性引擎 replan 摘要的 `result[:300]` 原是与 `UPSTREAM_BRIEF_LEN` 脱节的
**重复字面量**（改 config 不生效）；本次一并改为 `[:config.UPSTREAM_BRIEF_LEN]`，消除不一致。

## 验证

- 离线：`test_fixes_unit.py` **T1**——加载器三态（无文件/合法/损坏）+ `_o_*` 覆盖/缺失/非法兜底
  + 无 toml 时内置默认齐全（零回归）；`test_dag_unit.py` 未回归。
- 冒烟：`python test_planner_smoke.py`（import 链 + tomllib 加载未断）。
- 端到端（可选）：复制 example→`config.toml`，改 `[limits].dag_nodes = 3`，跑 Plan-Execute 确认节点被限。
