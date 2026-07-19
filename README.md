# 双路径调研智能体（ReAct / Plan-Execute）

**[简体中文](README.md) | [English](README.en.md)**

> 一个会"先想清楚再动手"的**联网调研智能体**。根据问题类型**动态路由**到两条执行路径——简单的边想边查（ReAct），复杂的先规划再执行（Plan-Execute）——并默认走 **DAG 分层并发 + 双层反思 + 计划落盘可恢复** 的生产级流程。全程流式，CLI REPL，基于 DeepSeek + 博查联网搜索。

本文档面向**想读懂内部原理、二次开发或贡献代码**的开发者。除"快速上手"外，后半部分逐层剖析了路由、DAG 流水线、双层反思、落盘恢复、LLM 调用层等全部机制。

> 起源：复现并深化两篇文章的思想——
> - 《为什么你的 AI Agent 总是跑偏？问题可能出在"不会做计划"》：引出 Plan-and-Execute + 动态重规划。
> - 《多智能体不是越多越强：我用100行Python，拆穿AI Agent协作的真相》：引出"通信协议决定上限"——原始需求直通车、结构化消息、双层审核。
>
> 本项目在这两条上做了架构深化（见 `docs/adr/`）。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [30 秒看懂：系统在做什么](#30-秒看懂系统在做什么)
- [技术架构](#技术架构)
- [端到端工作流（核心）](#端到端工作流核心)
- [核心机制逐个剖析](#核心机制逐个剖析)
- [设计原则（为什么这么写）](#设计原则为什么这么写)
- [快速上手](#快速上手)
- [REPL 命令](#repl-命令)
- [可调参数](#可调参数agentconfigpy)
- [项目结构](#项目结构)
- [测试](#测试)
- [如何扩展](#如何扩展)
- [架构决策记录（ADR）](#架构决策记录adr)
- [局限与路线图](#局限与路线图)

---

## 它解决什么问题

**纯 ReAct 智能体**"走一步看一步"，没有全局蓝图，容易在某个局部话题上越钻越深而跑偏——比如让它"调研今年电池技术的最新进展"，它会逮着"固态电池"一个词查到天荒地老，却忘了任务要的是全景式覆盖。

本项目用 **Plan-and-Execute + 动态重规划** 解决跑偏，并按问题类型自动选最合适的执行方式：

- **简单 / 探索型问题**（"今天天气""查个报错原因"）→ **ReAct**，一步到位，省事。
- **复杂 / 多维度任务**（"写份调研报告""多维度对比"）→ **Plan-Execute**，先出图纸再照图施工。

并在每个产出环节加**反思护栏**，在通信上保证**用户原始需求全程不丢**，把"每步都对、整体全错"的静默漂移拦在交付前。

---

## 30 秒看懂：系统在做什么

```
你输入一个问题
      │
      ▼
  路由分类器（1 次 LLM）──── 判断：简单题 or 复杂题？
      │
      ├── 简单 ──▶ ReAct：边想边查（≤6 轮 web_search）──▶ 直接回答
      │
      └── 复杂 ──▶ Plan-Execute（DAG 引擎，默认）：
                       ① 规划：把任务拆成带依赖的 DAG（哪几步、谁依赖谁）
                       ② 执行：按依赖分层，同层并发；每步产出由 Critic 把关，不达标重做
                       ③ 重规划：每层后据新信息调整剩余计划
                       ④ 汇总：写成报告，再由报告级 Reviewer 整体复核（漏了就补研重写）
                       全程落盘，中断可 /resume 续跑
```

---

## 技术架构

### 模块依赖图

依赖方向**单向无环**：上层编排，下层提供能力；任何模块都不反向依赖调用方。

```
                         repl.py  （CLI REPL 入口：路由分发 / 命令 / 异常隔离）
                            │
                       router.py  （问题类型理解 → react / plan）
                      ╱          ╲
            ┌──── ReAct ────┐   ┌─────────── Plan-Execute ───────────┐
            │               │   │                                     │
         react.py    plan_execute.py             orchestrator.py
         (顶层路径)    (线性引擎 /engine linear)    (DAG 引擎 · 默认)
            │               │                   ╱          ╲
            └────────┬──────┘            planner.py   execute_reflect.py
                     │                  (规划/重规划/    (执行 + 双层反思:
                  react_loop.py          拓扑/破环         子任务 Critic +
                (ReAct 循环深模块)        纯逻辑)           报告级 Reviewer)
                     │                                          │
   ┌─────────────────┴──────────────────────────────────────────┐
   llm.py        tools.py        dag_store.py    parsing.py    prompts.py
  (统一 Reply    (web_search     (计划/状态      (健壮 JSON     (共享
   + 懒构造      + 工具适配器)     落盘/恢复)      解析/去重)      prompt)
   client)                                                        │
                                                                config.py
                                                            (配置 + 常量)
```

### 模块职责一览

| 模块 | 职责 | 关键函数 |
|------|------|----------|
| `repl.py` | CLI 入口、命令解析、路由分发、单任务异常隔离 | `main`, `handle_task` |
| `agent/router.py` | 问题类型 → 路由（1 次 LLM） | `classify_route` |
| `agent/react.py` | ReAct 顶层路径（薄壳） | `run_react` |
| `agent/plan_execute.py` | 线性 Plan-Execute 引擎 | `run_plan_and_execute` |
| `agent/orchestrator.py` | DAG 引擎主流程 + 落盘 + 报告反思 | `run_plan_dag`, `_run_dag_async`, `_finalize` |
| `agent/planner.py` | DAG 规划/重规划/补研 + 纯逻辑（拓扑/破环） | `make_dag_plan`, `replan_dag`, `plan_missing`, `topo_layers`, `repair_and_acyclic` |
| `agent/execute_reflect.py` | 子任务执行 + 双层反思 | `execute_subtask`, `critic_review`, `report_review`, `run_with_reflection` |
| `agent/react_loop.py` | ReAct 循环深模块（三处复用） | `react_loop` |
| `agent/llm.py` | 统一 Reply/ToolCall + chat/chat_stream（懒构造 client） | `chat`, `chat_stream`, `Reply`, `ToolCall` |
| `agent/tools.py` | web_search + 工具协议适配器 | `web_search`, `web_search_tool`, `SEARCH_TOOL_SCHEMA` |
| `agent/dag_store.py` | 计划/状态/报告原子落盘与恢复 | `save_*`, `load_*`, `make_run_id` |
| `agent/parsing.py` | 健壮 JSON 解析、计划归一化、语义比较 | `parse_json_object`, `parse_json_array`, `plans_differ` |
| `agent/prompts.py` | 共享 prompt（子任务系统提示 / 汇总报告） | `SUBTASK_SYSTEM`, `summary_prompt` |
| `agent/config.py` | 配置 + 行为常量 + 控制台 UTF-8 修补 | `MODEL`, `MAX_*`, `clean_text` |

---

## 端到端工作流（核心）

本节是开发者最需要读懂的部分。沿着"用户敲一行字 → 最终报告"的完整路径，逐阶段说明发生了什么、调了哪些函数、数据长什么样。

### 阶段 0：REPL 入口（`repl.py`）

`main()` 维护两个会话状态：`stream`（是否流式，默认 on）、`engine`（`dag` 默认 / `linear`）。

每读一行输入：
- 斜杠命令（`/help` `/stream` `/engine` `/resume` `/runs` `/exit`）就地处理。
- 普通文本 → `config.clean_text()` 剔除孤立代理项（防 API 编码失败）→ `handle_task()`。
- **异常隔离**：单个任务的任何异常（含 `KeyboardInterrupt`）都被 `try/except` 兜住，**只结束当前任务，不退出会话**。

`handle_task(task, stream, engine)`：

```
route, reason = classify_route(task)          # 1 次分类 LLM
if route == "react":       run_react(task, stream)
elif engine == "linear":   run_plan_and_execute(task, stream)
else:                      run_plan_dag(task, stream, run_id)   # DAG 默认
```

### 阶段 1：路由分类（`router.py`）

`classify_route(task) -> (route, reason)` 用**一次非流式、关闭思考模式**的 LLM 调用（省钱提速），依据"步骤数是否确定 / 是否依赖中间结果 / 是否多维度"判断：

- → `react`：单一事实 / 即时问答 / 探索型。
- → `plan`：范围明确、多并列/依赖子目标。

输出严格 JSON `{"route","reason"}`。**兜底**：解析失败或非法值 → 默认 `plan`（复杂任务容错更高，宁可多规划）。

### 阶段 2a：ReAct 路径（`react.py` → `react_loop.py`）

`run_react` 只是个薄壳：设好顶层 system prompt 与 `MAX_REACT_TASK_ROUNDS=6`，调用深模块 `react_loop`。

**`react_loop` 是整个项目的循环深模块**（三个调用方复用：ReAct 顶层 / 线性引擎子任务 / DAG 引擎子任务）。它内部跑经典的"想—做—看"：

```
messages = [system, user]
for 轮 in range(max_rounds):
    reply = chat 或 chat_stream（按 stream 选）
    if reply 没有 tool_calls:          # 模型不再调工具 = 产出最终答案
        return reply
    # 否则：执行工具，把结果喂回上下文
    执行所有 tool_calls:
        - JSON 参数解析失败 → 不空调用，直接把"参数非法"回给模型让它下轮纠正
        - 解析成功 → execute_tool(name, arguments)，结果作为 tool 消息 append
# 轮数耗尽：去掉 tools 再调一次，逼模型基于已收集信息强制总结
```

关键点：**工具参数解析归 `react_loop` 管**，失败时**不带空参数去空搜**，而是提示模型纠正——避免浪费一次搜索。

### 阶段 2b：Plan-Execute · DAG 引擎（默认，重点）

入口 `run_plan_dag(task, stream, run_id, resume=False)`，分四步：

#### ① 规划：`planner.make_dag_plan(task)`

不是有序列表，而是**带 `depends_on` 的有向无环图**：

```
LLM 产出 JSON 数组 [{id, subtask, depends_on}, ...]
    ↓ extract_items        （兼容标准数组 / NDJSON / 散落对象三种格式）
    ↓ normalize_nodes      （抹平 str/dict 混合；缺/重 id 时补唯一 id）
    ↓ repair_and_acyclic   （悬空依赖剔除 + DFS 三色标记破环）
    ↓ 截断到 MAX_DAG_NODES
```

例：`调研钠离子与固态电池并对比` →
```
[t1] 调研钠离子现状      depends_on: []
[t2] 调研固态电池现状     depends_on: []
[t3] 综合对比            depends_on: [t1, t2]   ← 必须等 t1/t2 完成
```

`resume` 分支：从 `state.json` 恢复 `completed`/`remaining`；空/损坏 → 重新规划；原始 task 丢失 → 明确报错（无 task 无法重规划与汇总）。

#### ② 分层并发执行：`orchestrator._run_dag_async`

```
executed_total = len(completed)              # 预算起点（已完成的也计入）
while remaining and executed_total < MAX_DAG_NODES:
    layers = topo_layers(remaining, done=已完成id集合)   # Kahn BFS 分层
    for layer in layers:
        ▶ 同层节点 asyncio.gather(                          # 同层并发
              asyncio.to_thread(run_with_reflection, n, 上游摘要, task)
          )
        ▶ 每个节点结果 append 进 completed；executed_total++
        ▶ save_state（落盘，可中断恢复）
        ▶ 若 remaining 且有预算：
              new_remaining = replan_dag(task, completed, remaining, budget)
              if plans_differ(new_remaining, remaining):   # 语义级比较，防"只改措辞"误判
                  remaining = new_remaining; save_plan; break（重新分层）
```

**为什么同层并发用 `asyncio.to_thread` 而非 httpx/aiohttp？** 项目只依赖同步的 OpenAI SDK；用 `to_thread` 把同步调用包成可并发单元，**不引入新依赖**就拿到并发收益（控制改动面）。层间仍串行（下层依赖上层结果）。

#### ③ 单节点执行 + 子任务反思：`execute_reflect.run_with_reflection`

每个节点的执行是一个**执行 → 评估 → 不达标重执**的闭环：

```
for attempt in range(MAX_REFLECT_RETRIES + 1):       # 默认最多重执 2 次
    result = execute_subtask(subtask, 上游摘要, feedback, original_task=task)
    verdict = critic_review(subtask, result, original_task=task)
    if verdict["pass"]: return result
    feedback = f"问题：{issues}\n建议：{suggestion}"   # 带评语重执
# 重试耗尽 → 接受当前结果（打印警告）
```

- **`execute_subtask`**：拼好 user 内容 = `## 用户原始需求` + 子任务 + 上游摘要 + 反馈，调用 `react_loop`（非流式，避免多任务流式交错）。
- **`critic_review`**：Critic 从覆盖度/准确性/证据充分性/相关性四维评估，输出 `{pass, score, issues, suggestion}`；**score 是硬门槛**（≥`CRITIC_PASS_SCORE` 才放行，防"pass:true 但低分"的矛盾输出）；解析失败默认放行。

> **关键设计（ADR-0005）**：`execute_subtask` 与 `critic_review` 都带 `original_task`——用户原文直达每个执行/审核调用。Critic 同时看"子任务"和"原始需求"，充当用户代言人：产出若只满足子任务却偏离原文（漏约束、答非所问），判不通过。这是堵住"传话游戏"式静默丢约束的关键。

#### ④ 报告级反思循环：`orchestrator._finalize`（ADR-0006，双层反思的下半层）

所有节点跑完后，汇编成最终报告，再过一道**整体复核**：

```
for attempt in range(MAX_REPORT_RETRIES + 1):       # 默认最多重写 1 次
    report = _summarize(task, completed, stream, feedback)
    verdict = report_review(task, report, completed)
    if verdict["pass"]: break
    if 重试预算用尽: 接受当前报告; break
    missing = verdict["missing_constraints"]
    if missing 且有节点预算:
        new_nodes = planner.plan_missing(task, missing, completed)   # 为缺口产补研节点
        completed = asyncio.run(_run_dag_async(task, new_nodes, completed, run_id))  # 复用执行器
    feedback = f"问题：{issues}\n建议：{suggestion}"   # 带反馈重写
save_report(run_id, report)
```

`report_review` 区分两类问题：
- **综合缺陷**（素材在、报告没写好，如"对比"章节没真做对照）→ 写进 `issues/suggestion`，**带反馈重写**即可。
- **覆盖缺口**（原文某约束既不在报告、也不在任何子任务素材里，如"单独列出量产时间表"压根没被规划成子任务）→ 列入 `missing_constraints`，**经 `plan_missing` 定向补研后再重写**。

> 子任务级 Critic 抓"单步错"，报告级 Reviewer 抓"整体错"——后者结构上无法被前者替代（所有子任务各自通过，拼起来仍可能漏约束）。两层共同形成**"原文对齐链"：executor → critic → report**。

### 阶段 2c：Plan-Execute · 线性引擎（`/engine linear`）

`plan_execute.run_plan_and_execute` 是早期实现的线性版本（保留作对照与轻量场景）：

- `make_plan` 产出有序 `list[str]`（3-5 步，无依赖图）。
- 顺序执行 `execute_subtask(subtask, stream, original_task=task)`；每步后（除最后一步）`replan_if_needed` 据已完成结果调整剩余清单。
- 尾部同样跑报告级反思循环（与 DAG 引擎对称）。
- **注意**：线性引擎**没有子任务级 Critic**——它的质量闸只有报告级 Reviewer 一道。

### 阶段 3：产出与落盘

- ReAct 路径：直接把 `reply.content` 打给用户。
- Plan-Execute：最终报告流式打印 **并** 落盘到 `runs/<run_id>/report.md`。

---

## 核心机制逐个剖析

### 1. DAG 规划与校验（纯逻辑，可离线测试）

`planner.py` 把"需要 LLM 的规划"和"不依赖 LLM 的图逻辑"**在函数级分离**——后者可直接离线单测，无需 mock：

| 函数 | 作用 |
|------|------|
| `extract_items(text)` | 从模型输出抠节点，兼容标准 JSON 数组 / NDJSON / 散落对象 |
| `normalize_nodes(raw)` | 抹平 str/dict 混合输入为 `[{id, subtask, depends_on}]`；缺/重 id 补唯一 id |
| `repair_and_acyclic(nodes)` | 剔除悬空依赖与自依赖；**DFS 三色标记破环**（沿回边删成环依赖，退化为无环） |
| `topo_layers(nodes, done)` | **Kahn BFS 分层**：第 0 层无依赖，第 k 层依赖都在更浅层；`done` 集合让恢复场景下"已完成依赖"视为已满足 |

`replan_dag` 调整剩余计划时，`extra_valid_ids` 参数允许新节点依赖**已完成节点**（否则会被 `repair_and_acyclic` 当悬空依赖误删）。

### 2. 分层并发

`asyncio.gather` + `asyncio.to_thread` 实现同层节点并发、层间串行。**并发层统一非流式**（避免多任务流式输出交错混乱）；单节点层与最终报告才流式。

### 3. 上游注入（接力通信）

子任务执行时，其 `depends_on` 上游的**结果摘要**（截断到 `UPSTREAM_BRIEF_LEN=300` 字）被拼进 prompt——打破"子任务间信息隔离"。例如 `t3 对比` 执行时能看到 `t1`、`t2` 的结论，不必重复劳动。

这是文章所说"黑板+接力混合拓扑"里的**接力**部分：上游真实结果（而非规划者的转述）流给下游。

### 4. 双层反思

- **子任务级 Critic**（`critic_review`）：每步产出后评估，不达标带评语重执（≤`MAX_REFLECT_RETRIES=2`）。
- **报告级 Reviewer**（`report_review`）：最终报告整体复核，综合缺陷带反馈重写、覆盖缺口定向补研（≤`MAX_REPORT_RETRIES=1`）。

两层都**对齐原始需求**（ADR-0005），且 Critic/Reviewer 是独立 `chat()` 调用、**只看结果不看执行心路**——这种上下文隔离带来更强的批判性（审核者不背"自己刚写的"包袱）。

### 5. 原始需求直通车（ADR-0005）

用户原文 `task` **原样下发**到 executor 与 critic 的 prompt（不止 planner 用一次就丢）。文章原话：规划者拆出的 instruction 是"导航"，用户原文是"地图"——导航算错时执行者还能据地图自校正。成本只是多几百 token，远廉价于返工重跑。

### 6. 计划落盘与中断恢复（`dag_store.py`）

每次 run 在 `runs/<run_id>/` 实时原子写入三件套：

| 文件 | 内容 |
|------|------|
| `plan.json` | 当前 DAG 计划（replan 后更新） |
| `state.json` | `{task, completed:[{id,subtask,result}], remaining:[节点], layer}` |
| `report.md` | 最终报告 |

- **原子写**：先写 `.tmp` 再 `os.replace`；Windows 上目标文件被占用时降级直写并警告，不中断主流程。
- **`run_id` 跨进程稳定**：`md5(task)[:8] + 时间戳`，**不用内置 `hash()`**（后者每次进程启动随机化，同一任务跨进程哈希不同，无法恢复历史 run）。
- **`/resume <run_id>`**：加载已完成状态，`topo_layers(done=已完成id)` 跳过已完成节点继续，**不重复烧 token**。

### 7. 统一 Reply 形状 + 懒构造 client + 当前日期注入（`llm.py`，ADR-0001/0003/0007）

- **统一形状**：`chat` / `chat_stream` 都返回 `Reply(content, tool_calls, finish_reason)`；`ToolCall(id, name, raw_arguments)`。OpenAI SDK 的 `.choices[0].message.*` 结构被封在 `llm.py` 内部，**调用方再也不碰 SDK 对象**。这让测试可以打 `chat`（消费方命名空间）造假，比注入假 SDK client 更稳。
- **懒构造**：`_get_client()` 首次调用才创建 OpenAI client 并缓存——import 期无网络副作用（`import agent` 后 client 仍是 `None`）。
- **流式 tool_calls 累积**：`_ToolCallBuf` 按 `index` 聚合 id/name/arguments 片段（用 `+=` 幂等，兼容个别兼容层重复/分片发送）；重试时已打印内容不重复输出。
- **思考模式**：`chat`/`chat_stream` 支持 `thinking` 开关（DeepSeek reasoning）；规划/分类/抽取类调用关闭它（`extra_body: thinking.disabled`，省钱提速），ReAct 循环与报告生成开启。
- **重试**：指数退避（`2**attempt` 秒），默认 3 次；显式 `timeout=120s` 防流式半开连接挂死。
- **当前日期集中注入**：`chat`/`chat_stream` 在构造请求前经 `_inject_date` 把"当前日期：YYYY年M月D日（星期X）"注入 system 消息（已有则合并，否则插入）。模型从此知道"今天"，不再对"今天/今年/最新"幻觉（曾经把"今天"答成 2025，实际是 2026）；路由/规划/执行/反思/报告全覆盖（ADR-0007）。

### 8. 工具层（`tools.py`）

- **`web_search(query)`**：博查 Web Search API（DeepSeek 官方联网搜索供应方，国内直连）。`freshness=oneYear`、`summary=True`（长摘要）、`count=MAX_SEARCH_RESULTS`；防御性处理 `data:null`/缺层；单条结果截断 500 字防撑爆上下文；返回格式化的 `[n] 标题/时间/来源/摘要`。
- **`web_search_tool(name, arguments)`**：**工具协议适配器**——把 LLM 的 `(name, arguments dict)` 翻译成 `web_search(query)`。`web_search` 保持单一职责（只懂搜索），不被通用工具协议污染。**将来加新工具时，各自有各自的适配器**，在 `react_loop` 的 `execute_tool` 回调里分发。
- **`SEARCH_TOOL_SCHEMA`**：function-calling 的 tools schema。

### 9. 健壮的 JSON 解析（`parsing.py`）

模型即使被要求"只输出 JSON"，也常包 ```json 围栏或加废话。`_extract_json` 依次：剥围栏 → 直接 `json.loads` → 正则抠容器再 `loads`。

`plans_differ` 是**语义级比较**（剥标点/空格/大小写后比对）——LLM 重述时措辞总在变，逐字 `!=` 会每轮都误判"计划已调整"，白烧一次重规划。

### 10. 健壮性护栏（遍布全栈）

| 护栏 | 位置 |
|------|------|
| 所有循环有硬上限 | ReAct 6 轮 / 子任务 ReAct 3 轮 / 反思重试 2 次 / 报告重写 1 次 / DAG 8 节点 |
| LLM 重试 + 显式超时 | `llm.chat`/`chat_stream`（120s，指数退避 3 次） |
| DAG 校验 | 环检测破环、悬空依赖剔除、id 修复、恢复时已完成依赖视为满足 |
| 工具参数解析失败兜底 | 不带空 query 去空搜，提示模型纠正 |
| 预算护栏 | `_run_dag_async` 用 `executed_total`（含 replan/补研新增）防无限膨胀 |
| 密钥缺失启动即失败 | `os.environ[...]` → `KeyError`（fail-fast，而非推迟成 401） |
| 原子落盘 | `.tmp` + `os.replace`，崩溃不留半个 JSON |
| REPL 异常隔离 | 单任务异常不退出会话 |

---

## 设计原则（为什么这么写）

这些是项目反复打磨后确立的原则，贡献代码前请理解它们（详见 `docs/adr/`）：

1. **不引框架**：裸 Python + OpenAI SDK。文章原话"框架只是把这一百行封装得漂亮些"——手写一遍，每个装饰器背后是什么都清清楚楚。
2. **统一形状封 seam**：OpenAI SDK 结构封在 `llm.py` 内，调用方只接触 `Reply`（ADR-0001）。
3. **深模块**：`react_loop` 一个循环吃掉三个调用方，调用方只传旋钮（轮数/流式/system prompt/工具）（ADR-0002）。
4. **纯逻辑 / IO 分离**：`planner` 的拓扑/破环/归一化不依赖 LLM，可直接离线测。
5. **按业务生命周期拆模块**：`planner`（规划）/ `execute_reflect`（执行+反思）/ `orchestrator`（编排）三分（ADR-0004）。
6. **原始需求直通车**：原文常驻每次执行/审核调用（ADR-0005）。
7. **双层反思**：子任务 Critic + 报告 Reviewer，对齐原文形成完整防线（ADR-0006）。
8. **YAGNI 严格执行**：砍掉一切 speculative 设计（injectable client 工厂、`Node` dataclass、字面 `Blackboard` 类、过度抽象）——见 ADR-0003 的"未采纳项"。
9. **fail-fast 优于静默降级**：密钥缺失即报错，不推迟成 401 / 空搜索。
10. **依赖单向无环**：上层编排、下层供能，绝不反向。

---

## 快速上手

### 环境要求

- Python 3.11+
- DeepSeek API Key（[申请](https://platform.deepseek.com/)）
- 博查搜索 API Key（[申请](https://open.bochaai.com/)）

### 安装

```bash
cd Agentic-WebResearch
python -m venv .venv

# 激活（Windows PowerShell）
.\.venv\Scripts\Activate.ps1
# 或（Windows CMD）：  .\.venv\Scripts\activate.bat
# 或（Linux/macOS）：  source .venv/bin/activate

pip install -r requirements.txt
```

`requirements.txt`：

```
openai>=1.0
requests>=2.28
python-dotenv>=1.0
```

### 配置

项目根目录创建 `.env`：

```env
DEEPSEEK_API_KEY=sk-你的deepseek密钥
BOCHA_API_KEY=sk-你的博查密钥
DEEPSEEK_MODEL=deepseek-v4-flash
```

> 密钥缺失会在启动时直接报错（fail-fast）。`DEEPSEEK_MODEL` 可换成你账号可用的型号（如 `deepseek-chat`）。

**行为参数**（可选，默认开箱即用）：要调停止条件/上限而不改源码，复制模板：

```bash
cp config.example.toml config.toml   # 编辑 config.toml 即生效
```

`.env` 管密钥与模型，`config.toml` 管行为参数（二者分离）；缺失的键自动回退内置默认。详见 [可调参数](#可调参数configexampletoml)。

### 运行

```bash
python repl.py
```

直接输入问题即可：

```
============================================================
  双路径调研智能体（ReAct / Plan-Execute）· 全程流式
  模型：deepseek-v4-flash   Plan 引擎：DAG 并行+反思
============================================================

你> 今天北京天气怎么样
🧭 路由：ReAct（单一事实查询，即时问答）
🔍 搜索：北京今天天气
今天白天晴，最高气温 7°C ...

你> 调研2025年钠离子电池和固态电池进展并对比
🧭 路由：Plan-Execute（多子目标复杂任务）
📁 run_id: 20260719-164507-237c15be
DAG 计划：
   [t1] 调研钠离子电池技术进展
   [t2] 调研固态电池技术进展
   [t3] 综合对比  ⇐ 依赖 ['t1', 't2']
===== 第 1 层 · 并发执行 2 个节点 =====
   ...
```

### 试这些问题

- **ReAct 会接的**：`今天上海天气怎么样` / `宁德时代最新股价` / `什么是固态电池`
- **Plan-Execute 会接的**：`调研2025年钠离子电池和固态电池进展并对比` / `写一份关于今年新能源汽车市场的两千字报告`

---

## REPL 命令

| 命令 | 作用 |
|------|------|
| `/help` | 显示帮助 |
| `/stream on\|off` | 开关流式输出（默认 on） |
| `/engine dag\|linear` | 切换 Plan 路径引擎：`dag`=DAG 并行+反思（默认），`linear`=线性旧版 |
| `/resume <run_id>` | 恢复中断的任务（跳过已完成节点继续） |
| `/runs` | 列出所有历史 run |
| `/exit` `/quit` `Ctrl+C` | 退出 |

---

## 可调参数（`config.example.toml`）

所有停止条件与上限都可在 `config.toml` 里调（无需改源码）。完整清单（带注释）见
[`config.example.toml`](config.example.toml)，分组如下：

| 分组 | 含义 | 代表键 |
|---|---|---|
| `[limits]` | 循环迭代上限（智能体的"停止条件"） | `dag_nodes` `react_task_rounds` `plan_steps` `reflect_retries` `report_retries` |
| `[review]` | 通过阈值 + 审查截断 | `critic_pass_score` `critic_result_chars` `report_review_chars` |
| `[llm]` | 模型调用 | `retries` `timeout` `max_tokens` `report_max_tokens` |
| `[search]` | 联网搜索 | `max_results` `timeout` `content_chars` `freshness` `upstream_brief_chars` |

复制即用：`cp config.example.toml config.toml`。缺失键回退内置默认（`agent/config.py`），
非法值启动时告警并兜底——不崩。密钥与模型仍在 `.env`。详见 [ADR-0008](docs/adr/0008-external-config-toml.md)。

---

## 项目结构

```
Agentic-WebResearch/
├── repl.py                      # CLI REPL 入口
├── requirements.txt
├── .env                         # 密钥与模型配置（不入库）
├── config.example.toml          # 行为参数模板（复制为 config.toml 覆盖，不入库）
├── CONTEXT.md                   # 领域词汇表
├── README.md
│
├── agent/                       # 核心包
│   ├── config.py                #   配置 + 行为常量
│   ├── llm.py                   #   统一 Reply/ToolCall + chat/chat_stream（懒构造 client）
│   ├── parsing.py               #   健壮 JSON 解析（数组/对象，去重，语义比较）
│   ├── prompts.py               #   共享 prompt（子任务系统提示 / 汇总报告）
│   ├── tools.py                 #   web_search + 工具适配器
│   ├── router.py                #   问题类型 → 路由
│   ├── react_loop.py            #   ReAct 循环深模块（被三路径复用）
│   ├── react.py                 #   ReAct 顶层路径
│   ├── plan_execute.py          #   线性 Plan-Execute 引擎（/engine linear）
│   ├── planner.py               #   DAG 规划/重规划/补研 + 拓扑/破环纯逻辑
│   ├── execute_reflect.py       #   子任务执行 + 双层反思（Critic + 报告 Reviewer）
│   ├── orchestrator.py          #   DAG 引擎编排 + 落盘/恢复 + 报告反思循环
│   └── dag_store.py             #   计划/状态/报告原子持久化
│
├── test_dag_unit.py             # 离线单测（DAG 逻辑 / 环检测 / 拓扑 / 反思解析 / 落盘重载）
├── test_fixes_unit.py           # 离线单测（LLM 修复点 / passthrough / report_review / plan_missing）
├── test_planner_smoke.py        # 真实 API 冒烟（规划器连通 + DAG 规划，省 token）
│
├── docs/adr/                    # 架构决策记录
│   ├── 0001-unify-llm-reply.md
│   ├── 0002-consolidate-react-loop.md
│   ├── 0003-deferred-candidates.md
│   ├── 0004-split-plan-dag.md
│   ├── 0005-raw-requirement-passthrough.md
│   ├── 0006-report-level-reviewer.md
│   ├── 0007-current-date-injection.md
│   └── 0008-external-config-toml.md
│
└── runs/                        # 运行时落盘（自动生成）
    └── <run_id>/
        ├── plan.json
        ├── state.json
        └── report.md
```

---

## 测试

```bash
# 离线单测（不打真实 API，秒级）—— DAG 纯逻辑 / 环检测 / 拓扑分层 / 反思解析 / 落盘重载
python test_dag_unit.py

# 离线单测 —— LLM 修复点 + 原始需求直通 + 报告 Reviewer + 补研规划
python test_fixes_unit.py

# 真实 API 冒烟（省 token，只调一次规划器）—— DeepSeek 连通 + DAG 规划
python test_planner_smoke.py
```

端到端验证直接 `python repl.py` 跑真实问题。

**测试约定**：不依赖 pytest，每个 `test_*.py` 是可独立 `python` 运行的脚本，内部用 monkeypatch 打 `chat`/`react_loop` 造假，断言关键行为。纯逻辑（`topo_layers`/`repair_and_acyclic` 等）无需 mock，直接离线测。

---

## 如何扩展

### 加一个新工具（如 `read_url`）

1. 在 `tools.py` 实现工具函数（如 `read_url(url) -> str`）+ 其 schema + 适配器（`read_url_tool(name, arguments)`）。
2. 把 schema 加进传给 `react_loop` 的 tools 列表（如 `SEARCH_TOOL_SCHEMA + [READ_URL_SCHEMA]`），并在 `execute_tool` 回调里按 `name` 分发。
3. 子任务 system prompt（`prompts.SUBTASK_SYSTEM`）里告诉模型何时用新工具。

工具协议是 `(name: str, arguments: dict) -> str`——任何符合此签名的可调用对象都能挂上。

### 换模型 / 换供应商

`config.py` 的 `DEEPSEEK_MODEL` 可 `.env` 覆盖。若要换非 DeepSeek 供应商，改 `DEEPSEEK_BASE_URL` 与 `.env` 密钥即可（用的是 OpenAI 兼容接口）。**真出现多 client / 多供应商需求**时，再考虑 `llm.py` 的 injectable client 工厂（ADR-0003 显式暂缓项）。

### 加新的执行路径

在 `router.py` 的 `VALID_ROUTES` 加路由值，扩 `classify_route` prompt，在 `repl.handle_task` 里分发到新引擎。路由是纯数据驱动的，加一条路径不动现有两条。

### 调反思强度

改 `config.py` 的 `MAX_REFLECT_RETRIES`（子任务）、`MAX_REPORT_RETRIES`（报告）、`CRITIC_PASS_SCORE`（放行阈值）。提高 `CRITIC_PASS_SCORE` = 更严质量闸。

---

## 架构决策记录（ADR）

每个重要决策（含**被拒绝的方案及理由**）都落在 `docs/adr/`，贡献前请先读：

| ADR | 主题 |
|-----|------|
| [0001](docs/adr/0001-unify-llm-reply.md) | 统一 Reply 形状，把 OpenAI SDK 封在 seam 内 |
| [0002](docs/adr/0002-consolidate-react-loop.md) | 收敛三处 ReAct 循环为 `react_loop` 深模块 |
| [0003](docs/adr/0003-deferred-candidates.md) | 架构审查候选实施记录（含显式暂缓项及重提时机） |
| [0004](docs/adr/0004-split-plan-dag.md) | 拆 `plan_dag` 上帝模块为 planner / execute_reflect / orchestrator |
| [0005](docs/adr/0005-raw-requirement-passthrough.md) | 原始需求直通车（executor/critic 对齐用户原文） |
| [0006](docs/adr/0006-report-level-reviewer.md) | 报告级 Reviewer（双层反思下半层，重写+必要时补研） |
| [0007](docs/adr/0007-current-date-injection.md) | 当前日期集中注入（LLM seam 层，防时效性幻觉） |
| [0008](docs/adr/0008-external-config-toml.md) | 行为参数外部可配置化（config.toml，密钥与行为分离） |

新概念统一登记在 [`CONTEXT.md`](CONTEXT.md) 领域词汇表。

---

## 局限与路线图

**当前局限**（部分为显式 YAGNI 暂缓，见 ADR-0003）：

- **工具单一**：只有 `web_search`（`execute_tool` 回调已支持挂任意工具）。
- **无跨会话记忆**：REPL 每个任务独立，无多轮上下文累积。
- **报告导出**：仅落盘 `report.md`，无格式转换/分享。
- **并发层无流式**：DAG 同层并发时 echo=off（避免交错），单节点层与报告才流式。
- **路由无缓存**：每个任务 1 次分类调用。
- **无打包**：非 pip-installable，无 `pyproject.toml`。
- **测试覆盖偏逻辑**：router/react/plan_execute 执行路径只有真实 API 冒烟，无离线测试。

**可能的下一步**（非承诺，按需取舍）：

- 加 `read_url`（读全文）/ 计算器 / 代码执行等工具。
- Critic → Replan 信号回流（让子任务 Critic 发现的"规划级漂移"直接触发重规划）。
- pytest 迁移 + CI。
- 跨会话记忆 / 报告导出。

---

## 许可

本项目采用 [MIT 许可证](LICENSE)。

> Copyright (c) 2026 **jidechao/AES(GuangDian)**。
