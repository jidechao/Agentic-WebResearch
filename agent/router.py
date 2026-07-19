# -*- coding: utf-8 -*-
"""
问题类型理解 / 路径路由。

依据文章 04 DECISION 的判断标准并扩展：
- 走 ReAct        ：步骤数不确定、高度依赖中间结果、探索性强；
                    或单一事实 / 即时问答 / 简单查询。
- 走 Plan-Execute ：范围明确、有多个并列或有依赖关系的子目标；
                    如调研报告、多步骤数据处理、需覆盖多个维度的分析。
"""

from .llm import chat
from .parsing import parse_json_object

VALID_ROUTES = ("react", "plan")

_CLASSIFY_PROMPT = """你是一个任务路由分类器。判断下面这个用户任务，应该走哪条执行路径：

【ReAct 路径】适用于：
- 步骤数不确定、高度依赖中间结果、探索性强的任务（下一步该干嘛取决于上一步查到什么）；
- 单一事实查询、即时问答、简单问题。
  例：「今天北京天气怎么样」「某公司最新股价」「XX 是什么」「查一下报错原因」。

【Plan-Execute 路径】适用于：
- 范围明确、有多个并列或有依赖关系的子目标的复杂任务；
- 需要先想清楚"要覆盖哪几块"的全景式任务。
  例：「调研报告」「综述」「多维度对比分析」「多步骤数据处理」「写一份 X 字的报告」。

只输出一个 JSON 对象，格式严格如下，不要输出任何多余文字、不要用代码块包裹：
{"route": "react" 或 "plan", "reason": "一句话理由"}

任务：__TASK__"""


def classify_route(task: str) -> tuple[str, str]:
    """返回 (route, reason)。解析失败 / 非法值时兜底为 plan（复杂任务容错更高）。"""
    reply = chat(
        [{"role": "user", "content": _CLASSIFY_PROMPT.replace("__TASK__", task)}],
        thinking=False,
    )
    data = parse_json_object(reply.content)

    if not data:
        return "plan", "分类结果解析失败，默认走 Plan-Execute"

    route = str(data.get("route", "")).strip().lower()
    reason = str(data.get("reason", "")).strip() or "（无理由）"

    if route not in VALID_ROUTES:
        return "plan", f"非法路由值「{route}」，默认走 Plan-Execute"
    return route, reason
