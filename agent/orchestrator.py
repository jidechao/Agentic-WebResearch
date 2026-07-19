# -*- coding: utf-8 -*-
"""
Orchestrator（编排）：DAG 引擎的主流程。

把 planner（规划/重规划）与 execute_reflect（执行+反思）串成完整流程：
    make_dag_plan → 分层并发执行（同层 asyncio 并发）→ 分层 Replan → 汇总报告。
并负责计划/状态/报告的落盘与中断恢复（dag_store）。

依赖方向（单向无环）：
    orchestrator → planner / execute_reflect / dag_store / llm
"""

import asyncio

from . import config, dag_store
from .execute_reflect import report_review, run_with_reflection
from .llm import chat_stream
from .parsing import plans_differ
from .planner import make_dag_plan, plan_missing, repair_and_acyclic, replan_dag, topo_layers
from .prompts import summary_prompt


def _briefs_for(node: dict, completed: list[dict]) -> list[dict]:
    """取本节点 depends_on 对应的上游结果摘要。"""
    by_id = {c["id"]: c for c in completed}
    return [by_id[d] for d in node["depends_on"] if d in by_id]


async def _run_dag_async(task: str, nodes: list[dict], completed: list[dict], run_id: str) -> list[dict]:
    """分层并发执行剩余节点，返回完成后的 completed 列表。

    预算护栏：executed_total 记录累计执行过的节点数（含 replan 新增的），
    防止 replan 无限补充节点导致膨胀——这是 completed 只增不减时
    len(completed) < MAX 无法约束的盲区。
    """
    remaining = list(nodes)
    layer_idx = 0
    executed_total = len(completed)  # 已完成的也算入预算
    while remaining and executed_total < config.MAX_DAG_NODES:
        done = {c["id"] for c in completed}
        layers = topo_layers(remaining, done=done)
        replanned = False
        for layer in layers:
            if not layer or executed_total >= config.MAX_DAG_NODES:
                break
            layer_idx += 1
            print(f"\n===== 第 {layer_idx} 层 · 并发执行 {len(layer)} 个节点 =====")
            for n in layer:
                deps = n["depends_on"]
                dep_note = f"（依赖 {deps}）" if deps else ""
                print(f"   ▶ [{n['id']}] {n['subtask']}{dep_note}")

            # 同层并发（同步执行体用 to_thread 包装）
            # task 作为 original_task 透传给 executor+critic（原始需求直通车，ADR-0005）
            results = await asyncio.gather(
                *[
                    asyncio.to_thread(run_with_reflection, n, _briefs_for(n, completed), task)
                    for n in layer
                ]
            )
            for n, res in zip(layer, results):
                completed.append({"id": n["id"], "subtask": n["subtask"], "result": res})
                executed_total += 1
                print(f"   ✅ [{n['id']}] 完成（{len(res)} 字）")

            remaining = [n for n in remaining if n["id"] not in {c["id"] for c in completed}]
            dag_store.save_state(run_id, completed, remaining, layer_idx, task=task)  # 落盘

            # 分层 Replan：还有剩余且有预算时才调整；仅在确有变化时重建分层
            if remaining and executed_total < config.MAX_DAG_NODES:
                budget = config.MAX_DAG_NODES - executed_total
                new_remaining = replan_dag(task, completed, remaining, budget)
                # 语义级比较（去标点/空格），而非逐字 !=——避免模型仅改措辞就误判"计划已调整"
                if plans_differ(
                    [n["subtask"] for n in new_remaining],
                    [n["subtask"] for n in remaining],
                ):
                    print("   🔁 计划已根据新信息调整")
                    remaining = new_remaining
                    dag_store.save_plan(run_id, remaining)
                    replanned = True
                    break  # replan 后回到 while 重新分层
        if not replanned:
            # 本轮 layers 全部跑完且未触发 replan → remaining 已空或达预算，结束
            break
    if remaining:
        print(f"\n⚠️ 已达节点预算上限（{config.MAX_DAG_NODES}），剩余 {len(remaining)} 个节点未执行")
    return completed


def _summarize(task: str, completed: list[dict], stream: bool, feedback: str = "") -> str:
    brief = [
        {"subtask": c["subtask"], "result": c["result"]} for c in completed
    ]
    final = chat_stream(
        [{"role": "user", "content": summary_prompt(task, brief, feedback=feedback)}],
        thinking=True,
        max_tokens=config.REPORT_MAX_TOKENS,
        echo=stream,
    )
    if final.finish_reason == "length":
        print("\n⚠️ 报告已达 max_tokens 上限被截断，如需完整内容可调大 max_tokens。")
    return final.content


def _print_dag(nodes: list[dict]) -> None:
    print("DAG 计划：")
    for n in nodes:
        deps = f"  ⇐ 依赖 {n['depends_on']}" if n["depends_on"] else ""
        print(f"   [{n['id']}] {n['subtask']}{deps}")


def _finalize(task: str, completed: list[dict], run_id: str, stream: bool) -> str:
    """报告级反思循环（双层反思下半层，ADR-0006）：生成 → report_review →
    不达标则（覆盖缺口先定向补研 + ）带反馈重写，最多 MAX_REPORT_RETRIES 次；
    落盘最终报告。"""
    feedback = ""
    report = ""
    for attempt in range(config.MAX_REPORT_RETRIES + 1):
        print(f"\n{'🔄 据复核意见重写报告' if attempt else '正在撰写最终报告'} ...\n")
        report = _summarize(task, completed, stream, feedback=feedback)
        verdict = report_review(task, report, completed)
        missing = verdict.get("missing_constraints") or []
        tag = "✓" if verdict["pass"] else "✗"
        miss_note = f"；覆盖缺口 {missing}" if missing else ""
        print(
            f"🧐 报告复核 {tag} 评分 {verdict['score']}/10{miss_note}"
            + (f"：{verdict['issues'][:60]}" if not verdict["pass"] and verdict["issues"] else "")
        )
        if verdict["pass"]:
            break
        if attempt >= config.MAX_REPORT_RETRIES:
            print("⚠️ 报告反思重试已达上限，接受当前报告")
            break
        # 覆盖缺口：预算内先定向补研，completed 增长后重写才有新素材
        if missing and (config.MAX_DAG_NODES - len(completed)) > 0:
            print(f"🔁 定向补研 {len(missing)} 个覆盖缺口 ...")
            new_nodes = plan_missing(task, missing, completed)
            if new_nodes:
                completed = asyncio.run(_run_dag_async(task, new_nodes, completed, run_id))
        feedback = f"问题：{verdict['issues']}\n建议：{verdict['suggestion']}"
    if not stream:
        print(report)  # 流式已由 chat_stream echo；非流式补打整份报告
    dag_store.save_report(run_id, report)
    return report


def run_plan_dag(
    task: str,
    stream: bool = True,
    run_id: str | None = None,
    resume: bool = False,
) -> str:
    """DAG 并行 Plan-Execute 主流程。

    run_id 由调用方生成（含时间戳）；resume=True 时从 state.json 恢复，
    跳过已完成节点，不重复执行。
    """
    if run_id is None:
        run_id = dag_store.make_run_id(task, "manual")

    completed: list[dict] = []
    resumed = False
    if resume and dag_store.has_state(run_id):
        state = dag_store.load_state(run_id) or {}
        completed = state.get("completed", [])
        nodes = state.get("remaining", [])
        task = task or state.get("task", "")  # 恢复时回填原始任务，供重规划与汇总

        # completed 与 remaining 均空 → state 无效，转重新规划
        if not completed and not nodes:
            print(f"runs/{run_id} 的执行状态为空或已损坏，改为重新规划")
            nodes = None
        # 恢复后 task 仍为空 → 无法汇总/重规划，明确报错
        elif not task.strip():
            print(f"⚠️ runs/{run_id} 中未保存原始任务，无法恢复（请重新发起任务）")
            return ""
        else:
            resumed = True
            # 恢复出的 remaining 也需剔除悬空依赖
            nodes = repair_and_acyclic([dict(n) for n in nodes])
            print(f"已从 runs/{run_id} 恢复：已完成 {len(completed)} 个节点，剩余 {len(nodes)} 个")
            if not nodes:
                print("所有节点已完成，直接生成报告")
                return _finalize(task, completed, run_id, stream)
    else:
        nodes = None

    if not resumed:
        nodes = make_dag_plan(task)
        dag_store.save_plan(run_id, nodes)
        _print_dag(nodes)
        dag_store.save_state(run_id, completed, nodes, 0, task=task)

    completed = asyncio.run(_run_dag_async(task, nodes, completed, run_id))
    return _finalize(task, completed, run_id, stream)
