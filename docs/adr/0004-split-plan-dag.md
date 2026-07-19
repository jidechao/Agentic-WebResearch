# ADR-0004: 拆分 plan_dag 上帝模块为 planner / execute_reflect / orchestrator

- 状态：已接受
- 日期：2026-07-18
- 决策者：与用户 grilling 后共同确认

## 背景

`plan_dag.py` 曾横切 7 种职责（DAG 生成/校验、拓扑、执行、反思、重规划、
持久化、编排），直接依赖 4 个兄弟模块，测试只能戳私有名
（`_repair_and_acyclic`、`_execute_with_reflection`）。interface 与 implementation
几乎一样宽——浅。

## 决策

按**业务生命周期**（规划 → 执行 → 编排）拆为三个模块，替换 `plan_dag.py`：

- **`agent/planner.py`**：`make_dag_plan` / `replan_dag` + 纯逻辑
  `topo_layers` / `repair_and_acyclic` / `normalize_nodes` / `extract_items`。
- **`agent/execute_reflect.py`**：`execute_subtask` / `critic_review` / `run_with_reflection`。
- **`agent/orchestrator.py`**：`run_plan_dag` + 分层并发编排 + 汇总 + 落盘。

依赖方向单向无环：`orchestrator → planner / execute_reflect → react_loop`。

## 关键取舍（grilling 中确认）

1. **按业务生命周期拆（方案 B），不按 IO 纯度拆（方案 A）**。规划/执行/编排三阶段
   贴合领域语言；纯逻辑虽不单独成模块，但在函数级保持无 LLM 依赖，
   离线可测性不损失。
2. **`reflection` 不拆成 executor + critic**："反思"是执行→评估→重试的复合闭环，
   三步内聚不可分；拆开会撕裂闭环。模块名 `execute_reflect` 同时体现执行与反思两层。
3. **编排层命名 `orchestrator`**（非 plan_dag / dag_engine）。
4. **纯逻辑公开化**：`repair_and_acyclic` 等去掉下划线——它们被测试与 orchestrator
   依赖，已是事实上的公开 interface（interface 是测试面）。
5. **删除旧 `plan_dag.py`**，不留门面。

## 后果

- 拓扑/破环等纯逻辑可离线单测，无需 mock LLM。
- 测试不再戳私有名。
- 真实 API 端到端验证通过（4 节点并发 + 反思 + 强制总结 + 分层 Replan + 落盘）。

## 关联

- 完成 [[0003-deferred-candidates]] 中的候选 3。
- 建立在 [[0001-unify-llm-reply]]、[[0002-consolidate-react-loop]] 之上。
