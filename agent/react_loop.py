# -*- coding: utf-8 -*-
"""
ReAct 循环深模块（候选 1 收敛产物）。

把"想—做—看"循环、工具执行、轮数上限、强制总结收敛为一个深模块：
    三个调用方（react 顶层路径 / plan_execute 线性引擎子任务 / execute_reflect DAG 引擎子任务）
    只传旋钮（max_rounds / stream / system_prompt / 工具），不再各自复制循环。

interface 窄：react_loop + 一个工具回调约定。
implementation 深：循环、tool_calls 判断、assistant 消息重建、JSON 解析兜底、
                    轮数耗尽强制总结、流式/非流式分支，全部封在内部。
"""

import json

from .llm import Reply, chat, chat_stream


def _assistant_message(reply: Reply) -> dict:
    """把模型这一轮的回复（含 tool_calls）重建为 OpenAI assistant 消息。

    这是唯一需要 OpenAI 消息结构的地方，所以转换逻辑收在这里，
    而不是给 ToolCall 数据类挂方法（避免数据模型污染）。
    """
    return {
        "role": "assistant",
        "content": reply.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.raw_arguments},
            }
            for call in reply.tool_calls
        ],
    }


def _execute_tools(reply: Reply, messages: list, execute_tool) -> None:
    """执行本轮所有 tool_calls，把 assistant 消息与工具结果 append 回 messages。

    JSON 解析归这里：失败时直接把"参数非法"回给模型（不调用 execute_tool），
    让模型下一步自行纠正，而不是带空参数去空调用。
    """
    messages.append(_assistant_message(reply))
    for call in reply.tool_calls:
        try:
            arguments = json.loads(call.raw_arguments)
        except json.JSONDecodeError:
            print(f"   ⚠️ 工具参数解析失败（{call.name}），已提示模型纠正")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": "工具调用参数不是合法 JSON，未能执行。请重新组织参数后再调用。",
                }
            )
            continue
        result = execute_tool(call.name, arguments)
        messages.append({"role": "tool", "tool_call_id": call.id, "content": result})


def react_loop(
    user_content: str,
    *,
    system_prompt: str,
    tools: list,
    execute_tool,
    max_rounds: int,
    stream: bool,
) -> Reply:
    """跑一个 ReAct 循环，直到模型不再调用工具或达到轮数上限。

    参数：
        user_content   用户侧输入（任务 / 子任务文本，调用方已拼好上下文）。
        system_prompt  该路径的系统提示（顶层 / 子任务各不相同）。
        tools          function-calling 的 tools schema。
        execute_tool   工具执行回调：(name: str, arguments: dict) -> str。
        max_rounds     最大 ReAct 轮数（硬上限，防失控）。
        stream         True 用流式（逐 token 打印），False 用非流式。

    返回：统一 Reply（content 为最终文本；通常 tool_calls 为空 = 已收尾）。
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    def _call(msgs, use_tools):
        """按 stream 标志选择流式/非流式调用。"""
        if stream:
            return chat_stream(msgs, tools=use_tools, thinking=True, echo=True)
        return chat(msgs, tools=use_tools, thinking=True)

    for _ in range(max_rounds):
        reply = _call(messages, tools)

        # 没有调用工具 = 已经产出最终答案
        if not reply.tool_calls:
            return reply

        _execute_tools(reply, messages, execute_tool)

    # 轮数耗尽：不丢弃已收集信息，去掉 tools 再调一次，逼模型基于已有上下文强制总结
    print("\n（已达最大轮数，基于已收集信息总结）")
    messages.append(
        {"role": "user", "content": "请停止搜索，直接基于以上已获得的信息，总结并给出回答。"}
    )
    return _call(messages, None)
