# -*- coding: utf-8 -*-
"""
execute_reflect：子任务的执行 + 反思闭环。

"反思"是一个复合动作（执行 → 评估 → 不达标带评语重试），三步内聚不可分，
所以同处一个模块（模块名同时体现"执行"与"反思"两层意思）。

interface：
    execute_subtask(subtask, upstream_briefs, feedback)  执行单个子任务（ReAct 循环）
    critic_review(subtask, result)                        评估产出质量
    run_with_reflection(node, upstream_briefs)           执行 + 反思闭环（有限重试）

执行循环本身已收敛进 react_loop 深模块；本模块负责上游注入、反思编排。
"""

import json

from . import config
from .llm import chat
from .parsing import parse_json_object
from .prompts import SUBTASK_SYSTEM
from .react_loop import react_loop
from .tools import SEARCH_TOOL_SCHEMA, web_search_tool


def execute_subtask(
    subtask: str,
    upstream_briefs: list[dict],
    feedback: str = "",
    original_task: str = "",
) -> str:
    """同步执行单个子任务（ReAct 循环），可被 asyncio.to_thread 包装并发。

    upstream_briefs: [{subtask, result}] —— 本节点 depends_on 的上游结果摘要。
    feedback:        反思打回时的改进意见（issues + suggestion）。
    original_task:   用户原始任务。非空时原样前置给执行者（原始需求直通车，
                     见 ADR-0005）：规划者拆出的 subtask 是导航，原文是地图，
                     导航算错时执行者仍能据原文自校正。
    """
    # 原始需求直通车：原文常驻每次执行调用，防规划者拆解时丢约束（传话游戏）
    origin_text = ""
    if original_task:
        origin_text = (
            "## 用户原始需求（务必满足，不可遗漏其中任何约束）\n"
            f"{original_task}\n\n"
        )

    upstream_text = ""
    if upstream_briefs:
        lines = [
            f"【上游「{b['subtask']}」的结论摘要】\n{b['result'][: config.UPSTREAM_BRIEF_LEN]}"
            for b in upstream_briefs
        ]
        upstream_text = (
            "\n\n以下来自你已完成的关联子任务，可作为背景参考（不要重复劳动，聚焦本子任务）：\n"
            + "\n\n".join(lines)
        )

    feedback_text = ""
    if feedback:
        feedback_text = (
            f"\n\n上一次产出未通过质量检查，请针对性改进：\n{feedback}"
        )

    # 循环已收敛进 react_loop 深模块；并发层统一非流式（避免多任务流式交错）
    reply = react_loop(
        origin_text + subtask + upstream_text + feedback_text,
        system_prompt=SUBTASK_SYSTEM,
        tools=SEARCH_TOOL_SCHEMA,
        execute_tool=web_search_tool,
        max_rounds=config.MAX_REACT_ROUNDS,
        stream=False,
    )
    return reply.content or "（未能完成该子任务）"


# ================================================================ 反思（Critic）
_CRITIC_PROMPT = """你是一个严格的质量审查员。评估下面这个子任务的调研产出质量。
__ORIGIN__子任务：__SUBTASK__

产出内容：
__RESULT__

从四个维度评估：覆盖度（是否答全了子任务）、准确性（是否有事实/逻辑错误）、证据充分性（是否有数据/来源支撑）、相关性（是否跑题）。
只输出一个 JSON 对象，不要输出多余文字，不要用代码块包裹：
{"pass": true或false, "score": 1到10的整数, "issues": "主要问题（没有则空字符串）", "suggestion": "改进建议（没有则空字符串）"}
评分 >= __THRESHOLD__ 视为 pass。"""


def critic_review(subtask: str, result: str, original_task: str = "") -> dict:
    """评估单个子任务产出，返回 {pass, score, issues, suggestion}。解析失败默认放行。

    original_task 非空时注入"原始需求"段并对齐评分（ADR-0005）：Critic 不只对照
    子任务，还对照用户原文——否则规划者拆错时，执行者忠实完成错的指令也会被判通过。
    """
    # 原始需求直通车：Critic 兼任"用户代言人"，产出偏离原文即判不通过
    origin = (
        f"原始需求（你是用户代言人，产出若偏离它即判不通过）：\n{original_task}\n\n"
        if original_task
        else ""
    )
    prompt = (
        _CRITIC_PROMPT.replace("__ORIGIN__", origin)
        .replace("__SUBTASK__", subtask)
        .replace("__RESULT__", result[: config.CRITIC_RESULT_LEN])
        .replace("__THRESHOLD__", str(config.CRITIC_PASS_SCORE))
    )
    response = chat([{"role": "user", "content": prompt}], thinking=False)
    data = parse_json_object(response.content)
    if not data:
        return {"pass": True, "score": config.CRITIC_PASS_SCORE, "issues": "", "suggestion": ""}

    try:
        score = int(data.get("score", config.CRITIC_PASS_SCORE))
    except (TypeError, ValueError):
        score = config.CRITIC_PASS_SCORE
    # score 是硬门槛，pass 字段仅作参考：两者取与，避免模型给出 pass:true 但低分的矛盾输出被放行
    is_pass = bool(data.get("pass", score >= config.CRITIC_PASS_SCORE)) and score >= config.CRITIC_PASS_SCORE
    return {
        "pass": is_pass,
        "score": score,
        "issues": str(data.get("issues", "") or ""),
        "suggestion": str(data.get("suggestion", "") or ""),
    }


# ================================================================ 报告级 Reviewer（双层反思下半层，ADR-0006）
_REPORT_REVIEW_PROMPT = """你是最终报告的整体审查员，是用户在系统内的代言人。
判断这份报告是否完整满足用户的【原始需求】，而非 merely 把子任务结果拼起来。

原始需求：__TASK__

可用的子任务素材（已完成，报告理应基于它们）：
__COMPLETED__

报告：
__REPORT__

从这些维度评估：
- 约束覆盖：原始需求的每条约束是否都在报告中有体现；
- 综合 quality：对比/归纳是否真做了对照（而非把素材罗列一遍）；
- 准确性 / 证据充分性 / 相关性。

关键区分：把"原始需求里有、但既不在报告里、也不在上方任何子任务素材里"的约束列入
missing_constraints（这些是覆盖缺口，需要补研究）；其余问题（素材有但报告没写好）写进
issues/suggestion（重写即可）。

只输出一个 JSON 对象，不要输出多余文字，不要用代码块包裹：
{"pass": true或false, "score": 1到10的整数, "issues": "主要问题（没有则空字符串）", "suggestion": "改进建议（没有则空字符串）", "missing_constraints": ["需要补研究的约束", ...]}
没有缺失项时 missing_constraints 给空数组。评分 >= __THRESHOLD__ 视为 pass。"""


def report_review(task: str, report: str, completed: list[dict]) -> dict:
    """评估最终报告是否完整满足原始需求，返回
    {pass, score, issues, suggestion, missing_constraints}。

    missing_constraints：原始需求里有、但既不在报告里、也不在任何 completed 素材里的约束
    ——覆盖缺口，需补研究（空列表 = 纯综合缺陷，重写即可）。解析失败默认放行。
    与 critic_review 同模式（chat + parse_json_object），仅扩展一个字段。
    """
    completed_brief = json.dumps(
        [
            {
                "subtask": c.get("subtask", ""),
                "result": (c.get("result") or "")[: config.UPSTREAM_BRIEF_LEN],
            }
            for c in completed
        ],
        ensure_ascii=False,
        indent=2,
    )
    prompt = (
        _REPORT_REVIEW_PROMPT.replace("__TASK__", task)
        .replace("__COMPLETED__", completed_brief)
        .replace("__REPORT__", report[: config.REPORT_REVIEW_LEN])
        .replace("__THRESHOLD__", str(config.CRITIC_PASS_SCORE))
    )
    response = chat([{"role": "user", "content": prompt}], thinking=False)
    data = parse_json_object(response.content)
    if not data:
        return {
            "pass": True,
            "score": config.CRITIC_PASS_SCORE,
            "issues": "",
            "suggestion": "",
            "missing_constraints": [],
        }

    try:
        score = int(data.get("score", config.CRITIC_PASS_SCORE))
    except (TypeError, ValueError):
        score = config.CRITIC_PASS_SCORE
    is_pass = bool(data.get("pass", score >= config.CRITIC_PASS_SCORE)) and score >= config.CRITIC_PASS_SCORE
    raw_missing = data.get("missing_constraints") or []
    if not isinstance(raw_missing, list):
        raw_missing = [raw_missing]
    missing = [str(m) for m in raw_missing if str(m).strip()]
    return {
        "pass": is_pass,
        "score": score,
        "issues": str(data.get("issues", "") or ""),
        "suggestion": str(data.get("suggestion", "") or ""),
        "missing_constraints": missing,
    }


def run_with_reflection(node: dict, upstream_briefs: list[dict], original_task: str = "") -> str:
    """执行 + 反思闭环：不达标带评语重执，最多 MAX_REFLECT_RETRIES 次。

    original_task 透传给 executor 与 critic（原始需求直通车，见 ADR-0005）。
    """
    subtask = node["subtask"]
    feedback = ""
    result = ""
    for attempt in range(config.MAX_REFLECT_RETRIES + 1):
        result = execute_subtask(subtask, upstream_briefs, feedback=feedback, original_task=original_task)
        review = critic_review(subtask, result, original_task=original_task)
        tag = "✓" if review["pass"] else "✗"
        print(f"   🧐 [{node['id']}] Critic {tag} 评分 {review['score']}/10"
              + (f"：{review['issues'][:60]}" if not review["pass"] and review["issues"] else ""))
        if review["pass"]:
            return result
        if attempt < config.MAX_REFLECT_RETRIES:
            feedback = f"问题：{review['issues']}\n建议：{review['suggestion']}"
    print(f"   ⚠️ [{node['id']}] 反思重试已达上限，接受当前结果")
    return result
