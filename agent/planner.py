# -*- coding: utf-8 -*-
"""
Planner（规划者）：把复杂任务变成可执行的 DAG，并在执行中动态调整。

interface（公开，可被离线单测）：
    纯逻辑（无 LLM、无 IO）：
        extract_items / normalize_nodes / repair_and_acyclic / topo_layers
    LLM 驱动：
        make_dag_plan / replan_dag

设计要点：
    - 纯逻辑与 IO 分离在函数级——topo/repair/normalize/extract 不依赖 LLM，
      可直接离线测试，无需 mock。
    - repair_and_acyclic 的 extra_valid_ids 允许 replan 节点依赖已完成节点
      （见 ADR-0003 之前的 R2 修复）。
"""

import json
import re

from . import config
from .llm import chat
from .parsing import parse_json_array


# ================================================================ 纯逻辑：提取 / 归一化
def extract_items(text: str) -> list:
    """从模型输出提取 DAG 节点项，兼容三种格式：
    1. 标准 JSON 数组 [{...}, {...}]
    2. NDJSON（每行一个独立 JSON 对象，无外层数组）—— 模型照 prompt 示例输出时最常见
    3. 多个独立 JSON 对象分散在文本中
    """
    if not text:
        return []
    # 先按标准数组解析
    arr = parse_json_array(text)
    if arr:
        return arr
    # 退化：逐行 / 逐个 {...} 对象解析（NDJSON 或散列对象）
    items: list = []
    for m in re.finditer(r"\{[^{}]*\}", text, re.DOTALL):
        try:
            obj = json.loads(m.group())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    return items


def normalize_nodes(raw: list | None) -> list[dict]:
    """把 LLM 输出归一化为节点 dict 列表：[{id, subtask, depends_on}]。

    保留模型给出的 id（depends_on 依赖它）；缺失/重复时再补唯一 id。
    """
    nodes: list[dict] = []
    seen: set[str] = set()

    def fresh_id() -> str:
        k = 1
        while f"t{k}" in seen:
            k += 1
        return f"t{k}"

    for item in raw or []:
        if isinstance(item, str):
            subtask, deps, nid = item.strip(), [], None
        elif isinstance(item, dict):
            subtask = str(
                item.get("subtask") or item.get("task") or item.get("description") or ""
            ).strip()
            deps = item.get("depends_on") or item.get("deps") or []
            if not isinstance(deps, list):
                deps = [deps]
            nid = item.get("id")
        else:
            continue
        if not subtask:
            continue
        # id 缺失或重复 → 重新分配；否则保留（保证 depends_on 引用有效）
        if not nid or not str(nid).strip() or str(nid) in seen:
            nid = fresh_id()
        nid = str(nid)
        seen.add(nid)
        nodes.append({"id": nid, "subtask": subtask, "depends_on": deps})
    return nodes


# ================================================================ 纯逻辑：校验 / 破环 / 拓扑
def repair_and_acyclic(nodes: list[dict], extra_valid_ids: set[str] | None = None) -> list[dict]:
    """修复依赖引用 + 破环，保证返回的是合法 DAG。

    - depends_on 指向不存在的 id → 剔除；
    - 存在环 → 沿 DFS 回边删除成环依赖，退化为无环（保持可拓扑）。

    extra_valid_ids：除当前节点外，额外认可的合法依赖 id（如 replan 时已
    完成节点的 id）。这些依赖保留在 depends_on 里（供 topo 的 done 机制识别），
    但不参与当前集合的建图与破环。
    """
    if not nodes:
        return []
    valid_ids = {n["id"] for n in nodes}
    keep_external = extra_valid_ids or set()
    for n in nodes:
        n["depends_on"] = [
            d for d in n["depends_on"]
            if (d in valid_ids and d != n["id"]) or d in keep_external
        ]

    # DFS 三色标记破环（仅对当前集合内的依赖建图；外部已完成依赖不参与）
    color = {n["id"]: 0 for n in nodes}
    # 记录每个节点的外部合法依赖（已完成节点），破环后需并回，避免被覆盖丢弃
    external_deps = {
        n["id"]: [d for d in n["depends_on"] if d in keep_external] for n in nodes
    }
    adj = {n["id"]: [d for d in n["depends_on"] if d in valid_ids] for n in nodes}

    def dfs(u: str) -> None:
        color[u] = 1
        for v in list(adj[u]):
            if color[v] == 0:
                dfs(v)
            elif color[v] == 1:
                # u 依赖 v，而 v 在递归栈上 → 成环，删除这条回边
                adj[u].remove(v)
        color[u] = 2

    for n in nodes:
        if color[n["id"]] == 0:
            dfs(n["id"])
    for n in nodes:
        # 集合内（已破环）+ 集合外已完成依赖，合并且去重保序
        merged = adj[n["id"]] + [d for d in external_deps[n["id"]] if d not in adj[n["id"]]]
        n["depends_on"] = merged
    return nodes


def topo_layers(nodes: list[dict], done: set[str] | None = None) -> list[list[dict]]:
    """Kahn BFS 分层：第 0 层无依赖，第 k 层依赖都在更浅层。

    done: 已完成的节点 id 集合 —— 这些依赖视为已满足（恢复中断任务时，
    剩余节点的 depends_on 仍指向已完成节点，不在 nodes 里，需按已完成处理）。
    若存在无法分层的节点（理论上 repair_and_acyclic 已破环），抛 ValueError。
    """
    done = done or set()
    by_id = {n["id"]: n for n in nodes}
    # 入度只统计"未完成且存在于当前集合中"的依赖
    indeg = {
        n["id"]: len([d for d in n["depends_on"] if d not in done and d in by_id])
        for n in nodes
    }
    # 反向邻接：某节点被哪些节点依赖
    dependents: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for n in nodes:
        for d in n["depends_on"]:
            if d in by_id:  # 只挂在当前集合内的依赖
                dependents[d].append(n["id"])

    layers: list[list[dict]] = []
    ready = [nid for nid, deg in indeg.items() if deg == 0]
    processed = 0
    while ready:
        layer = [by_id[nid] for nid in ready]
        layers.append(layer)
        processed += len(layer)
        nxt: list[str] = []
        for nid in ready:
            for m in dependents[nid]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    nxt.append(m)
        ready = nxt

    if processed != len(nodes):
        raise ValueError("DAG 存在环或悬空依赖，无法拓扑分层")
    return layers


# ================================================================ 生成 DAG 计划
_PLAN_PROMPT = """你是一个经验丰富的任务规划专家。请把下面这个复杂任务，
拆解成 3-6 个具体的调研子任务，并组织成一个有向无环图（DAG）：
- 大多数"信息调研类"子任务彼此独立，depends_on 为空（可并行）；
- "对比 / 综合 / 归纳 / 写报告类"子任务，depends_on 应列出它所依赖的调研子任务 id；
- 覆盖任务要求的各个方面，不要在某个局部话题上过度深挖；
- 每个子任务必须是可以通过联网搜索完成的具体调研动作。

只输出一个 JSON 数组，每个元素形如：
{"id": "t1", "subtask": "调研 A 的现状", "depends_on": []}
{"id": "t2", "subtask": "调研 B 的现状", "depends_on": []}
{"id": "t3", "subtask": "对比 A 与 B", "depends_on": ["t1", "t2"]}
不要输出任何多余文字，不要用 markdown 代码块包裹。

任务：__TASK__"""


def make_dag_plan(task: str) -> list[dict]:
    """生成 DAG 计划，含校验 / 修复 / 破环 / 兜底。"""
    response = chat(
        [{"role": "user", "content": _PLAN_PROMPT.replace("__TASK__", task)}],
        thinking=False,
    )
    items = extract_items(response.content)
    nodes = repair_and_acyclic(normalize_nodes(items))
    if not nodes:
        print("DAG 计划解析失败，降级为单节点执行")
        nodes = [{"id": "t1", "subtask": task, "depends_on": []}]
    return nodes[: config.MAX_DAG_NODES]


# ================================================================ 分层 Replan
_REPLAN_PROMPT = """原始任务：__TASK__

已完成的子任务及结果摘要：
__COMPLETED__

剩余待执行的子任务（含依赖）：
__REMAINING__

请判断：基于已获得的信息，剩余计划是否仍然合理？保守优先——没有充分理由就不要改。
调整原则：
- 剩余任务已被前面结果覆盖 → 删除；
- 发现原始任务必需但遗漏的方向 → 补充（新节点给新 id）；
- 依赖关系不合理 → 调整 depends_on。

约束：剩余子任务最多 __BUDGET__ 条；保持 JSON 数组，元素形如
{"id": "t4", "subtask": "...", "depends_on": ["t1"]}
只输出 JSON 数组，不要多余文字，不要代码块。无需调整则原样输出剩余节点数组。"""


def replan_dag(task: str, completed: list[dict], remaining: list[dict], budget: int) -> list[dict]:
    """分层 Replan：根据最新结果调整剩余节点 DAG。"""
    if not remaining or budget <= 0:
        return remaining

    completed_brief = [
        {"id": c["id"], "subtask": c["subtask"], "result": c["result"][: config.UPSTREAM_BRIEF_LEN]}
        for c in completed
    ]
    prompt = (
        _REPLAN_PROMPT.replace("__TASK__", task)
        .replace("__COMPLETED__", json.dumps(completed_brief, ensure_ascii=False, indent=2))
        .replace("__REMAINING__", json.dumps(remaining, ensure_ascii=False, indent=2))
        .replace("__BUDGET__", str(budget))
    )
    response = chat([{"role": "user", "content": prompt}], thinking=False)
    items = extract_items(response.content)
    # replan 节点的 depends_on 可能指向已完成节点 id，
    # 把它们列为合法外部依赖，避免被 repair_and_acyclic 误删
    done_ids = {c["id"] for c in completed}
    nodes = repair_and_acyclic(normalize_nodes(items), extra_valid_ids=done_ids)
    if not nodes:
        return remaining
    return nodes[:budget]


# ================================================================ 覆盖缺口补研（报告级 Reviewer 触发）
_MISSING_PROMPT = """原始任务：__TASK__

下列约束在已完成的子任务中都没有覆盖（覆盖缺口），需要补充研究：
__MISSING__

请为这些约束补研，每个约束产出 1 个可独立执行的联网调研子任务（彼此独立，可并行）。
只输出一个 JSON 数组，元素形如：
{"id": "m1", "subtask": "具体可执行的调研动作", "depends_on": []}
不要输出多余文字，不要用 markdown 代码块包裹。"""


def plan_missing(task: str, missing_constraints: list[str], completed: list[dict]) -> list[dict]:
    """为覆盖缺口产出补研 DAG 节点（report_review 的 missing_constraints → 新节点）。

    补研节点彼此独立（depends_on 为空，可同层并行）；id 重打为 m* 杜绝与已完成 t*
    节点碰撞（_run_dag_async 的 by_id/done 逻辑依赖全局唯一 id）。预算由本函数用
    MAX_DAG_NODES - len(completed) 自检：耗尽则返回空列表（调用方据空列表跳过补研）。
    """
    budget = config.MAX_DAG_NODES - len(completed)
    if budget <= 0 or not missing_constraints:
        return []
    gaps = missing_constraints[:budget]
    prompt = (
        _MISSING_PROMPT.replace("__TASK__", task)
        .replace("__MISSING__", json.dumps(gaps, ensure_ascii=False, indent=2))
    )
    response = chat([{"role": "user", "content": prompt}], thinking=False)
    items = extract_items(response.content)
    nodes = normalize_nodes(items)
    # 重打 m* id + 强制独立依赖：补研节点语义上就是独立的，且杜绝 id 碰撞
    safe: list[dict] = []
    for i, n in enumerate(nodes[:budget], start=1):
        n["id"] = f"m{i}"
        n["depends_on"] = []
        safe.append(n)
    return safe
