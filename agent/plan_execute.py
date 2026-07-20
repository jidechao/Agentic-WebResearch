# -*- coding: utf-8 -*-
"""
Plan-Execute 路径：先出图纸，再照图施工 + 动态重规划。

保持原 Demo 的核心逻辑不变：
    make_plan → 逐步 execute_subtask → replan_if_needed（plans_differ）→ 汇总报告。
仅把"面向用户的正文产出"（子任务总结、最终报告）改为流式输出；
规划 / 重规划这类需要解析完整 JSON 的调用仍走非流式。
"""

import json

from . import config
from . import evidence
from .execute_reflect import report_review
from .llm import chat, chat_stream
from .parsing import normalize_plan, parse_json_array, plans_differ
from .planner import plan_missing
from .prompts import SUBTASK_SYSTEM, summary_prompt
from .react_loop import react_loop
from .tools import SEARCH_TOOL_SCHEMA, web_search_tool


# ---------------------------------------------------------------- STEP 01 规划者
def make_plan(task: str) -> list[str]:
    """把复杂任务拆解成一份有序的子任务清单。"""
    prompt = f"""你是一个经验丰富的任务规划专家。请把下面这个复杂任务，
拆解成 3-5 个具体的、有明确先后顺序的调研子任务，确保覆盖任务要求的各个方面，
不要在某个局部话题上过度深挖。
每个子任务必须是可以通过联网搜索完成的具体调研动作。
请只输出一个 JSON 字符串数组，例如：
["调研 A 的现状", "调研 B 的技术路线", "对比 A 与 B"]
不要输出任何多余的文字，不要用 markdown 代码块包裹。
任务：{task}"""

    response = chat([{"role": "user", "content": prompt}], thinking=False)
    raw = parse_json_array(response.content)
    plan = normalize_plan(raw)
    if not plan:
        # 兜底：解析失败时至少还能跑，但要让用户知道降级了
        print("计划解析失败，降级为单步执行")
        return [task]
    return plan[: config.MAX_PLAN_STEPS]


# ---------------------------------------------------------------- STEP 02 执行者
def execute_subtask(subtask: str, stream: bool = True, original_task: str = "") -> str:
    """针对单个子任务，用 ReAct 循环完成它（循环已收敛进 react_loop 深模块）。

    original_task 非空时原样前置给执行者（原始需求直通车，见 ADR-0005）。
    """
    origin_text = (
        "## 用户原始需求（务必满足，不可遗漏其中任何约束）\n"
        f"{original_task}\n\n"
        if original_task
        else ""
    )
    reply = react_loop(
        origin_text + subtask,
        system_prompt=SUBTASK_SYSTEM,
        tools=SEARCH_TOOL_SCHEMA,
        execute_tool=web_search_tool,
        max_rounds=config.MAX_REACT_ROUNDS,
        stream=stream,
    )
    return reply.content or "（未能完成该子任务）"


# ---------------------------------------------------------------- STEP 03 复盘逻辑
def replan_if_needed(
    original_task: str,
    completed: list[dict],
    remaining: list[str],
    budget: int,
) -> list[str]:
    """根据最新执行结果，判断是否需要调整剩余计划。"""
    if not remaining or budget <= 0:
        return remaining

    # 只喂结果摘要，避免上下文随轮数二次方膨胀
    completed_brief = [
        {"subtask": c["subtask"], "result": c["result"][: config.UPSTREAM_BRIEF_LEN]} for c in completed
    ]

    prompt = f"""原始任务：{original_task}
已完成的子任务及结果摘要：
{json.dumps(completed_brief, ensure_ascii=False, indent=2)}
剩余待执行的子任务：
{json.dumps(remaining, ensure_ascii=False)}
请判断：基于已获得的信息，剩余计划是否仍然合理？
调整原则（保守优先 —— 没有充分理由就不要改）：
- 剩余任务已被前面的结果覆盖 → 删除；
- 发现了原始任务必需但计划遗漏的方向 → 补充；
- 先后顺序明显不合理 → 调整。
约束：剩余子任务最多 {budget} 条。
只输出一个 JSON 字符串数组，不要输出任何多余文字，不要用代码块包裹。
如果无需调整，原样输出剩余子任务数组。"""

    response = chat([{"role": "user", "content": prompt}], thinking=False)
    raw = parse_json_array(response.content)
    new_remaining = normalize_plan(raw)
    if not new_remaining:
        return remaining  # 解析失败 → 沿用原计划
    return new_remaining[:budget]


# ---------------------------------------------------------------- STEP 04 主流程
def run_plan_and_execute(task: str, stream: bool = True) -> str:
    evidence.reset_evidence()  # 新任务重置证据池（单 run 作用域）
    plan = make_plan(task)
    print("初始计划：")
    for index, item in enumerate(plan, start=1):
        print(f"   {index}. {item}")

    completed: list[dict] = []
    i = 0
    while i < len(plan) and i < config.MAX_PLAN_STEPS:
        subtask = plan[i]
        print(f"\n正在执行第 {i + 1}/{len(plan)} 步：{subtask}")
        result = execute_subtask(subtask, stream=stream, original_task=task)
        print(f"\n[步骤 {i + 1} 完成]")
        completed.append({"subtask": subtask, "result": result})

        # 最后一步之后不需要重规划 —— 原代码这里在白烧 token
        is_last = i == len(plan) - 1
        if not is_last:
            old_remaining = plan[i + 1:]
            budget = config.MAX_PLAN_STEPS - (i + 1)  # 硬预算，杜绝无限膨胀
            new_remaining = replan_if_needed(task, completed, old_remaining, budget)
            if plans_differ(new_remaining, old_remaining):
                print("计划已根据新信息调整：")
                for idx, item in enumerate(new_remaining, start=i + 2):
                    print(f"   {idx}. {item}")
                plan = plan[: i + 1] + new_remaining

        i += 1

    # 报告级反思循环（双层反思下半层，ADR-0006）：生成 → report_review →
    # 不达标则（覆盖缺口先补研 + ）带反馈重写，最多 MAX_REPORT_RETRIES 次。
    feedback = ""
    report = ""
    for attempt in range(config.MAX_REPORT_RETRIES + 1):
        print(f"\n{'🔄 据复核意见重写报告' if attempt else '正在撰写最终报告'} ...\n")
        final = chat_stream(
            [{"role": "user", "content": summary_prompt(task, completed, feedback=feedback)}],
            thinking=True,
            max_tokens=config.REPORT_MAX_TOKENS,  # 2000 字中文约 3000+ token，默认值会截断
            echo=stream,
        )
        if final.finish_reason == "length":
            print("\n⚠️ 报告已达 max_tokens 上限被截断，如需完整内容可调大 max_tokens。")
        report = final.content
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
        # 覆盖缺口：预算内逐条补研，追加进 completed
        if missing and (config.MAX_PLAN_STEPS - len(completed)) > 0:
            print(f"🔁 定向补研 {len(missing)} 个覆盖缺口 ...")
            new_nodes = plan_missing(task, missing, completed)
            for n in new_nodes:
                if len(completed) >= config.MAX_PLAN_STEPS:
                    break
                s = n["subtask"]
                print(f"   ▶ 补研：{s}")
                completed.append(
                    {"subtask": s, "result": execute_subtask(s, stream=stream, original_task=task)}
                )
        feedback = f"问题：{verdict['issues']}\n建议：{verdict['suggestion']}"
    # 反思循环结束后执行一次引用后处理（P2 时机）：[sN]→连续[k]+参考列表。
    # 放在 print/return 前，保证非流式终端显示与返回值一致（含参考列表）。
    report = evidence.finalize_report(report)
    if not stream:
        print(report)  # 流式已由 chat_stream echo；非流式补打整份报告
    else:
        # 流式下 chat_stream 已 echo 原始正文（含 [sN]），这里补打参考章节。
        refs = evidence.references_section(report)
        if refs:
            print("\n" + refs)
        else:
            print("\n（本报告无引用标记，未生成参考列表）")
    return report
