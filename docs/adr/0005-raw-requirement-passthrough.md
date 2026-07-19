# ADR-0005: 原始需求直通车（Executor / Critic 对齐用户原文）

- 状态：已接受
- 日期：2026-07-19

## 背景

源文章《多智能体不是越多越强：我用100行Python，拆穿AI Agent协作的真相》的核心论点：
**多智能体系统的上限由通信协议决定，而非智能体数量**（"加智能体是加法，修通信是乘法"）。
文章自评"全文最值钱"的两条对策是：

- **§3.2 原始需求直通车** —— 用户原始需求必须**原封不动**塞进每个下游智能体的输入。
  规划者拆出的 instruction 是"导航"，用户原文是"地图"；导航可能算错，地图在手执行者还能自校正。
- **§3.5 审核者对齐原始需求** —— Critic 的输入必须**同时**包含"用户原始需求"和"步骤要求"。
  否则规划者拆错 → 执行者忠实执行错的指令 → Critic 判"通过"（因它确实完美符合那个错的指令），
  拦不住"每步都对、整体全错"的静默漂移。

逐行比对当前代码，发现这两条对策恰好是缺口：

| 调用点 | 原先拿到的 | 缺失的 |
|---|---|---|
| `execute_reflect.execute_subtask` | `subtask` + 上游摘要 + 反馈 | 顶层 `task` |
| `execute_reflect.critic_review` | `subtask` + `result` | 顶层 `task` |
| `plan_execute.execute_subtask`（线性引擎） | `subtask` | 顶层 `task` |

即：Planner 把 `task` 拆成 `subtask` 后，**原文就被丢掉了**——这正是文章 §3.1 的"传话游戏"。
多约束任务（"重点看 X、单独列出 Y、对比 Z"）一旦在拆解时丢约束，执行者无地图可校正，
Critic 也只对照子任务放行。

> 注：项目其余通信对策已达标（结构化消息见各 JSON prompt；黑板+接力骨架见
> [[0004-split-plan-dag]] 的 orchestrator；上下文隔离见 Critic 独立 `chat()` 调用）。
> 本 ADR 只补这唯一缺口。

## 决策

把顶层 `task` 作为 `original_task` 形参，**原样**注入 executor 与 critic 的输入：

- `execute_reflect.execute_subtask(subtask, upstream_briefs, feedback="", original_task="")`：
  在 `react_loop` 的 user 内容**最前面**前置 `## 用户原始需求（务必满足，不可遗漏其中任何约束）` 段。
- `execute_reflect.critic_review(subtask, result, original_task="")`：在 `_CRITIC_PROMPT` 里
  插入 `原始需求（你是用户代言人，产出若偏离它即判不通过）` 段——Critic 兼任用户代言人。
- `execute_reflect.run_with_reflection(node, upstream_briefs, original_task="")`：透传给上面两函数。
- `orchestrator._run_dag_async`：把已有的 `task` 作为第三位置实参传给 `run_with_reflection`
  （`asyncio.to_thread(run_with_reflection, n, _briefs_for(n, completed), task)`）。
- `plan_execute.execute_subtask(subtask, stream=True, original_task="")` + 主流程透传：
  线性引擎保持一致（线性引擎无 Critic，故只改 executor）。

### 设计约束

- **向后兼容**：`original_task=""`（默认）时不注入该段，行为与改动前完全一致——
  保降级路径（如 resume 时 task 回填失败、或未来其他调用点）不崩。
- **零新依赖、零架构改动**：不改模块边界（[[0004-split-plan-dag]] 维持）、不引数据类、
  不引框架。原文常驻每次执行/审核调用，多几百 token，远廉价于返工重跑。
- **离线可测**：`test_fixes_unit.py` 加 3 个断言（critic 注入 / executor 注入 / 空值兼容），
  `test_dag_unit.py` 的预算护栏测试顺带验证 orchestrator→执行点的透传。

## 未采纳项（防重提）

以下文章提到、但**不做**——理由均为 ADR-0003 既定或本文未带来新信息：

- **不引入字面 `Blackboard` dataclass**：`orchestrator` 已扮演黑板角色（持 `task`+`completed`，
  落盘于 `dag_store`）；ADR-0003 因同样理由暂缓了 `Node` dataclass。重提时机：node 结构
  频繁变 / 多人协作防拼错。
- **不给 DAG 节点加 `context` 字段**：当前的 `upstream_briefs` 注入的是**真实上游结果**，
  比规划者的 `context` 转述更丰富；文章的 `context` 是其弱化版。重提时机：出现需要
  规划期就固化的、与上游结果无关的背景信息。
- **不把 executor/critic 拆成独立模块**：[[0004-split-plan-dag]] 已定——隔离在 LLM 调用
  边界（每次 `chat()`/`react_loop` 全新上下文），不在 Python 模块边界。Critic 看不到
  执行者的 ReAct 心路，隔离已达成。
- **不迁移到 AutoGen/LangGraph**：文章原话"框架只是把这一百行封装得漂亮些"；
  当前裸实现已足够清晰，且本项目核心价值在 DAG 分层并发 + 反思 + 落盘恢复，
  非角色拓扑。

## 验证

- 离线：`python test_dag_unit.py`（12 项含透传断言）、`python test_fixes_unit.py`
  （含 P1/P2/P3 三个注入断言）—— 全绿。
- 冒烟：`python test_planner_smoke.py`（planner 未改，确认 import 链未断）。
- 端到端：`python repl.py` 跑多约束任务（如"调研…**重点对比成本与能量密度两个维度**，
  并**单独列出量产时间表**"），观察 Critic 能就"是否漏维度"打回。
