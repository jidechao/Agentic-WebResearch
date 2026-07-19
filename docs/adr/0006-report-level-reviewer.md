# ADR-0006: 报告级 Reviewer（双层反思下半层 · 重写 + 必要时补研）

- 状态：已接受
- 日期：2026-07-19

## 背景

[[0005-raw-requirement-passthrough]] 把"原始需求直通车"接到了 executor + critic，
整条执行链都对齐了用户原文——**唯独最终报告这一层没有 Reviewer**。
`orchestrator._summarize` / 线性引擎的 `chat_stream` 直接把 completed 拼成报告就返回。

这一层能抓的错，是子任务级 Critic **结构上抓不到**的：所有子任务各自通过，但拼起来的报告
仍漏约束。典型：

- **综合缺陷**：子任务分别查了"成本"和"能量密度"，但"对比"章节没在那两维上做对照
  （素材在、报告没写好）→ 重写即可。
- **覆盖缺口**：原文要"单独列出量产时间表"，planner 压根没拆这一步（根本没素材）→
  需要补研究。

handoff §4b 曾记录"双层反思"被推迟（当时选单层）。现在 passthrough 已就位，重新评估时，
报告级复核正是最后缺的对齐层——它把文章 §3.5"审核者是用户代言人"延伸到**交付物层**。

经 grilling，用户拍板失败处理为 **"重写 + 必要时补研"**：综合缺陷带反馈重写；覆盖缺口
定向补研后再重写。

## 决策

### 1. `report_review`（`agent/execute_reflect.py`，与 `critic_review` 同模块同模式）

`report_review(task, report, completed) -> dict`，复用 `chat` + `parse_json_object`，
**扩展 schema** 多一个 `missing_constraints` 字段：

```
{"pass": bool, "score": int, "issues": str, "suggestion": str, "missing_constraints": [str]}
```

reviewer 同时看到 `task`（原文）+ `completed` 摘要 + `report`，据此把"原文里有、但既不在
报告里、也不在任何 completed 素材里"的约束列入 `missing_constraints`（覆盖缺口）；其余
问题写进 `issues`/`suggestion`（综合缺陷）。解析失败默认放行（`pass=True, missing=[]`）。

### 2. `plan_missing`（`agent/planner.py`，与 `make_dag_plan`/`replan_dag` 同模块）

`plan_missing(task, missing_constraints, completed) -> list[dict]`：为覆盖缺口产出补研
DAG 节点。补研节点**彼此独立**（`depends_on=[]`，可同层并行）；id 重打为 `m1..mN`
**杜绝与已完成 `t*` 节点的 id 碰撞**（`_run_dag_async` 的 `by_id`/`done` 逻辑依赖全局
唯一 id）。自检预算：`MAX_DAG_NODES - len(completed) <= 0` 时直接返回空列表。

### 3. 报告级反思循环（lifts `_finalize` 出一层循环，两引擎对称）

DAG（`agent/orchestrator.py` 的 `_finalize`）与线性（`agent/plan_execute.py` 主流程尾部）
都改为：

```
for attempt in range(MAX_REPORT_RETRIES + 1):
    report = _summarize(task, completed, stream, feedback=feedback)
    verdict = report_review(task, report, completed)
    if verdict["pass"]: break
    if attempt >= MAX_REPORT_RETRIES: 接受当前报告; break
    if missing and 预算>0:
        new_nodes = plan_missing(task, missing, completed)
        # DAG：completed = asyncio.run(_run_dag_async(task, new_nodes, completed, run_id))
        # 线性：逐条 execute_subtask 后 append 进 completed
    feedback = f"问题：{issues}\n建议：{suggestion}"
save_report(report); return report
```

关键复用：DAG 的定向补研走既有 `_run_dag_async(task, new_nodes, completed, run_id)`——它
**本就接受预填的 `completed`**（为 resume 设计），内部 `executed_total` 硬顶 `MAX_DAG_NODES`，
补研天然受预算约束。`_summarize` / `summary_prompt` 加 `feedback` 形参（非空时追加
"上次报告的问题"段，并在要求里加"务必覆盖原始需求的每条约束"）。

### 4. 配置（`agent/config.py`）

- `MAX_REPORT_RETRIES = 1`（镜像 `MAX_REFLECT_RETRIES`；默认 1 = 最多 1 次重写+补研）。
- 阈值复用 `CRITIC_PASS_SCORE`（同 1–10 量表）。

## 防死循环 / 预算

- `MAX_REPORT_RETRIES=1` 硬顶报告重生次数 → 最坏路径：初稿 → 复核 → 补研+1 重写 → 复核 →
  接受。**有界**。
- 定向补研受 `MAX_DAG_NODES`（DAG）/ `MAX_PLAN_STEPS`（线性）双重节点预算约束；耗尽时
  `plan_missing` 返回空 / 线性循环跳出，报告据现有素材重写，仍不达标则接受（打印警告）。
- 每次 `report_review` = 1 次 LLM 调用，次数受 `MAX_REPORT_RETRIES+1` 约束。

## 未采纳项（防重提）

- **不无界补研**：报告复核不达标时最多补研一轮（受 `MAX_REPORT_RETRIES` 与节点预算双顶）。
  重提时机：证明 1 轮补研系统性不够（需数据支撑，非直觉）。
- **不把 `report_review` 独立成模块**：与 `critic_review` 同模式同层（都是 review），同处
  `execute_reflect` 更内聚；[[0004-split-plan-dag]] 的拆分粒度是"执行+反思"复合动作。
- **不把"覆盖缺口"信号回流给子任务级 replan**：报告级 `plan_missing` 已能补研，足够；
  把信号再下沉到 `_run_dag_async` 的层间 replan 属过度耦合（接近候选 2 的复杂度，已显式推迟）。

## 验证

- 离线：`python test_dag_unit.py`（12 项，未回归）+ `python test_fixes_unit.py`
  （含 R1/R2/R3：report_review 注入与解析、解析失败放行、plan_missing 预算与 m* id）—— 全绿。
- 冒烟：`python test_planner_smoke.py`（import 链未断）。
- 端到端（`python repl.py`，DAG 默认）：用带易漏约束的任务（"…**重点对比成本与能量密度**，
  **单独列出量产时间表**"），观察 `🧐 报告复核 ✗` → `🔁 定向补研 …` → 重写 → 复核 ✓。

## 与 ADR-0005 的关系

0005 让 executor + critic 对齐原文；0006 把同一原则延伸到**最终报告**，形成完整的
"原文对齐链"：executor → critic → report。双层反思（子任务级 + 报告级）至此闭环。
