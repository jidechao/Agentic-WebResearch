# CONTEXT.md — 领域词汇表

本文件命名项目里的核心概念，供架构审查与协作时使用统一的领域语言。
架构词汇（module / interface / depth / seam / adapter / leverage / locality）
见 `/codebase-design` 技能；本文件定义的是**本项目**的领域词。

## 执行路径（Router 的两种路由）

- **ReAct 路径**：简单 / 探索型问题的顶层"想—做—看"循环，直接面向用户产出答案。
  入口 `agent/react.py` 的 `run_react`。
- **Plan-Execute 路径**：复杂 / 多维度任务，先规划再执行。有两种引擎（见下）。

## 引擎（Plan-Execute 的两种实现，可 `/engine` 切换）

- **线性引擎** `agent/plan_execute.py`：按下标顺序逐条执行子任务 + 动态重规划。
- **DAG 引擎**（默认）：显式依赖建模 + 分层并发 + 反思 + 落盘恢复。由三个模块协作：
  - **`agent/planner.py`**：规划与重规划（`make_dag_plan` / `replan_dag`）+ 纯逻辑
    （`topo_layers` / `repair_and_acyclic` / `normalize_nodes` / `extract_items`，可离线单测）。
  - **`agent/execute_reflect.py`**：子任务的执行 + 反思闭环
    （`execute_subtask` / `critic_review` / `run_with_reflection`）。
  - **`agent/orchestrator.py`**：编排（`run_plan_dag`），把规划与执行串成完整流程 + 落盘。
  - 依赖方向单向无环：orchestrator → planner / execute_reflect → react_loop。

## 核心机制

- **ReAct 循环**：想（模型决策是否调工具）—做（web_search）—看（结果喂回上下文）。
  已收敛为深模块 `agent/react_loop.py`，被 ReAct 路径与两种引擎的子任务执行复用。
- **规划（Planner）**：把复杂任务拆解为子任务。线性版产出有序列表；DAG 版产出
  带 `depends_on` 的节点图。
- **拓扑分层（topo_layers）**：Kahn BFS，把 DAG 按依赖深度分层；同层节点并发、层间串行。
- **反思（Reflection / Critic）**：每个子任务产出后由 Critic 评估质量
  （`{pass, score, issues, suggestion}`），不达标带评语重执（≤ `MAX_REFLECT_RETRIES` 次）。
- **报告级 Reviewer（双层反思下半层）**：最终报告生成后再由 `report_review` 整体复核
  （对齐原始需求，schema 扩展 `missing_constraints`）。综合缺陷带反馈重写；覆盖缺口
  （原文约束无任何子任务覆盖）经 `plan_missing` 定向补研后再重写（≤ `MAX_REPORT_RETRIES` 次）。
  与子任务级 Critic 形成"原文对齐链"（executor→critic→report），见 ADR-0006。
- **重规划（Replan）**：线性版每步后、DAG 版每层后，根据已完成结果动态调整剩余计划。
- **上游注入**：DAG 子任务执行时附带其 `depends_on` 上游的结果摘要，打破子任务间信息隔离。
- **原始需求直通车**：顶层 `task` 原样下发给 executor 与 critic（不只在 planner 处用一次就丢）。
  规划者拆出的 subtask 是"导航"，用户原文是"地图"——导航算错时执行者据原文自校正，
  critic 据原文判整体是否跑偏（兼任"用户代言人"）。防"传话游戏"式静默丢约束
  （见 ADR-0005）。黑板（orchestrator 持 `task`+`completed`）由此向 executor/critic
  开放只读 `user_request`。
- **计划落盘**：DAG 的 plan/state/report 实时写入 `runs/<run_id>/`，支持中断后
  `/resume <run_id>` 跳过已完成节点恢复。

## 横切概念

- **Reply / ToolCall**：`agent/llm.py` 的统一返回形状（见 ADR-0001）。
- **当前日期注入**：`llm._inject_date` 在每次 `chat`/`chat_stream` 把"当前日期：YYYY年M月D日（星期X）"注入 system 消息（已有则合并），防模型对"今天/今年/最新"幻觉（见 ADR-0007）。
- **外部配置（config.toml）**：行为参数（停止条件 + 生成/上下文上限）从项目根 `config.toml` 覆盖，`config.example.toml` 是模板，`agent/config.py` 内置默认兜底；密钥与模型仍在 `.env`（见 ADR-0008）。
- **web_search 工具**：博查联网搜索；`tools.web_search_tool` 是它的工具协议适配器。
- **引用（Citation）**：报告标注证据来源的内联 [N] 标记 + 末尾参考列表；证据 ID（s1/s2/...）由 web_search 工具在解析博查结果时分配并写入全局证据池，URL/title 直取 API（零幻觉）；报告后处理把 [sN] 重映射为连续 [1]..[n] 并拼接参考列表（见 ADR-0009）。
- **路由（Router）**：`agent/router.py`，问题类型理解 → 分发到 ReAct / Plan-Execute。

## 决策记录

见 `docs/adr/`：0001 统一 Reply、0002 收敛 react_loop、0003 暂缓候选、
0004 拆分 plan_dag 上帝模块为 planner / execute_reflect / orchestrator、
0005 原始需求直通车（executor/critic 对齐用户原文）、
0006 报告级 Reviewer（双层反思下半层，重写 + 必要时补研）、
0007 当前日期集中注入（LLM seam，防时效性幻觉）、
0008 行为参数外部可配置化（config.toml，密钥与行为分离）。 0009 引用事实源锚定搜索工具层（Citation：工具层分配 ID，URL 直取 API，零幻觉）。
