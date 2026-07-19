# ADR-0003: 架构审查候选的实施记录

- 状态：已接受
- 日期：2026-07-18（2026-07-19 更新）

## 背景

架构审查（见 temp 目录 architecture-review-*.html）提出 5 个深化候选。
各候选的实施状态如下（候选 1/2 见独立 ADR；3/4/5 原暂缓，后陆续落地）：

- 候选 1（收敛 react_loop）：已实施，见 [[0002-consolidate-react-loop]]
- 候选 2（统一 Reply）：已实施，见 [[0001-unify-llm-reply]]
- 候选 3（拆 plan_dag）：已实施，见 [[0004-split-plan-dag]]
- 候选 4（可注入 client）：部分实施（仅懒构造，不做 injectable 全套）
- 候选 5（删脱节副本）：已实施

## 候选 3：拆开 plan_dag 上帝模块（~~Worth exploring~~ → **已完成**）

**已于 2026-07-18 实施，见 [[0004-split-plan-dag]]。** 按业务生命周期拆为
`planner.py` / `execute_reflect.py` / `orchestrator.py` 三模块，`plan_dag.py` 已删除。

## 候选 4：可注入 client（~~Worth exploring~~ → **部分完成**）

**已于 2026-07-19 收窄实施。** 经 grilling 后确认候选 4 是两半、价值不同：

- **(a) 消除 import 期副作用（已做）**：`llm.py` 删除模块级 `client = OpenAI(...)`，
  改为私有 `_get_client()` 懒构造 + 缓存；import 不再创建网络客户端
  （验证：import 后 `_client is None`，首次 chat 才构造）。`chat`/`chat_stream`
  内部改调 `_get_client()`。
- **(b) injectable client 全套（不做，YAGNI）**：未引入 `make_client()`/`set_client()`
  工厂。理由：测试已通过打 `chat`（消费方命名空间）造假，比注入假 SDK client 更稳定
  （Reply 是我们的形状，高层造假不易脆）；多 client / 换供应商是当前没有的需求。
- **stdout/stdin reconfigure（不动）**：保留在 `config.py` import 期。它是 idempotent、
  无外部资源、Windows GBK 控制台的必需补丁；挪到 `init_console()` 要在 4+ 入口显式
  调用，churn-for-little-gain。

重提 (b) 全套的时机：真出现多 client / 换供应商需求时，再加工厂。

## 候选 5：删除 plan_execute_agent.py 脱节副本（~~Speculative~~ → **已完成**）

**已于 2026-07-19 实施。** 删除 `plan_execute_agent.py`（422 行漂移副本），
`test_make_plan.py` repoint 为 `test_planner_smoke.py`（测 `agent.planner.make_dag_plan`）。

当初"暂缓"的理由是"保留作文章原版对照"，但 grilling 中确认该理由已不成立：
1. 它已不是原版——上轮边界修复（H2/M2/M3）被同步进了它，成了半同步漂移副本；
2. 原版已在 `backup-20260718-204931/plan_execute_agent.py` 完整保留，删工作区副本不丢东西。

连带处理：原 `test_make_plan.py` import 的是脱节副本的 `make_plan`，一并 repoint 到
真实实现 `agent.planner.make_dag_plan`（断言改为节点 dict 形状），保留其"省 token
真实 API 规划器冒烟"价值，改名 `test_planner_smoke.py`。`agent.config` 已统一处理
stdout 编码，原测试里手动的 `sys.stdout.reconfigure` 冗余，已去掉。

**结果**：生产入口唯一（`repl.py` → `agent` 包）；规划器冒烟指向真实实现。
