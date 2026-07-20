# -*- coding: utf-8 -*-
"""
ReAct 路径：面向独立完整任务的顶层"想—做—看"循环。

循环本身已收敛进 agent/react_loop.py（深模块）。
本模块只是顶层调用方：拼好顶层 system_prompt 与旋钮，调用 react_loop。
"""

from . import config
from . import evidence
from .react_loop import react_loop
from .tools import SEARCH_TOOL_SCHEMA, web_search_tool

_SYSTEM = (
    "你是一个联网调研助手。针对用户的问题，边思考边用 web_search 工具获取真实信息，"
    "再基于搜索结果给出准确回答。\n"
    "要求：\n"
    "1. 优先使用 web_search 工具获取真实信息，不要凭记忆编造；\n"
    "2. 回答时保留关键数据、时间和来源；引用证据时使用搜索结果里出现的 [sN] 标记，不要自编编号；\n"
    "3. 信息足够后直接给出回答，不要过度搜索。"
)


def run_react(task: str, stream: bool = True) -> str:
    """顶层 ReAct 循环：想—做—看，直到产出最终答案或轮数耗尽。"""
    evidence.reset_evidence()  # 新任务重置证据池（单 run 作用域）
    reply = react_loop(
        task,
        system_prompt=_SYSTEM,
        tools=SEARCH_TOOL_SCHEMA,
        execute_tool=web_search_tool,
        max_rounds=config.MAX_REACT_TASK_ROUNDS,
        stream=stream,
    )
    content = reply.content or "（未能完成该任务）"
    # 反思循环结束后、返回前执行一次引用后处理（P2 时机）：[sN]→连续[k]+参考列表
    content = evidence.finalize_report(content)
    # 流式已由 chat_stream 实时 echo；非流式在此补打，保证 /stream off 也能看到答案
    if not stream:
        print(content)
    else:
        # 流式下 chat_stream 已 echo 原始正文（含 [sN]），这里补打参考章节。
        refs = evidence.references_section(content)
        if refs:
            print("\n" + refs)
        else:
            print("\n（本回答无引用标记，未生成参考列表）")
    return content
