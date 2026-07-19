# -*- coding: utf-8 -*-
"""
统一的模型调用入口与返回形状。

- chat()        非流式，用于需要一次性拿到完整 JSON 的场景（分类 / 规划 / 重规划）。
- chat_stream() 流式，逐 token 打印正文，并按 index 累积流式 tool_calls。

两者都返回统一的 Reply，把 OpenAI SDK 的对象结构封在 seam 内部：
调用方只接触 reply.content / reply.tool_calls / reply.finish_reason，
不再伸手 .choices[0].message.*。
"""

import json
import time
from datetime import datetime
from typing import Any

from openai import OpenAI

from . import config

# 懒构造：不在 import 时创建网络客户端（消除 import 期副作用）。
# 首次 chat/chat_stream 调用时才实例化并缓存。
# 测试打的是 chat 本身（消费方命名空间），_get_client() 在测试里根本不触发。
_client = None


def _get_client() -> OpenAI:
    """懒构造并缓存模块级 OpenAI client。"""
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            # 显式超时，防止流式读取在半开连接上长时间阻塞。
            # 该 timeout 作用于连接与读取（含流式 chunk 间隙），超时抛 APIError 由重试/上层兜底。
            timeout=config.LLM_TIMEOUT,
        )
    return _client


class ToolCall:
    """一次工具调用的纯数据表示（OpenAI 原始字段，不预解析）。"""

    def __init__(self, call_id: str, name: str, raw_arguments: str):
        self.id = call_id
        self.type = "function"
        self.name = name
        self.raw_arguments = raw_arguments  # 原始 JSON 字符串；解析发生在执行工具那一步


class Reply:
    """统一的模型返回形状：content + tool_calls + finish_reason。"""

    def __init__(self, content: str, tool_calls: list | None, finish_reason: str | None = None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.finish_reason = finish_reason


def _sdk_tool_calls_to_reply(tool_calls) -> list[ToolCall]:
    """把 SDK 的 tool_calls（或非流式 message.tool_calls）转成统一 ToolCall 列表。"""
    result = []
    for tc in tool_calls or []:
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", "") if fn is not None else ""
        raw_args = getattr(fn, "arguments", "") if fn is not None else ""
        result.append(ToolCall(getattr(tc, "id", ""), name, raw_args))
    return result


def _build_kwargs(messages: list, tools, thinking: bool, max_tokens: int) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": config.MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        kwargs["tools"] = tools
    if not thinking:
        # 规划 / 抽取类任务不需要思考模式，省钱提速
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return kwargs


_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _today_line() -> str:
    """当前日期串（中文 + 星期），注入每次 LLM 调用，防模型对'今天/今年'幻觉。"""
    now = datetime.now()
    return f"当前日期：{now.year}年{now.month}月{now.day}日（{_WEEKDAYS[now.weekday()]}）。"


def _inject_date(messages: list) -> list:
    """在消息列表头部注入当前日期：已有 system 消息则合并进去，否则插一个 system 消息。

    集中在 LLM 调用 seam 注入（ADR-0001 的 seam 思路）——路由/规划/执行/反思/报告
    全部环节自动带上"今天"，未来新增 prompt 无需手动加、永不漏。
    """
    line = _today_line()
    if messages and messages[0].get("role") == "system":
        msgs = list(messages)
        head = dict(msgs[0])
        head["content"] = f"{line}\n" + (head.get("content") or "")
        msgs[0] = head
        return msgs
    return [{"role": "system", "content": line}, *messages]


def chat(messages: list, *, tools=None, thinking=False, max_tokens=config.LLM_MAX_TOKENS, retries=config.LLM_RETRIES) -> Reply:
    """非流式调用：返回统一 Reply（用于需解析 JSON / 判断 tool_calls 的场景）。"""
    messages = _inject_date(messages)
    kwargs = _build_kwargs(messages, tools, thinking, max_tokens)

    last_err = None
    for attempt in range(retries):
        try:
            completion = _get_client().chat.completions.create(**kwargs)
            choice = completion.choices[0]
            msg = choice.message
            return Reply(
                content=msg.content or "",
                tool_calls=_sdk_tool_calls_to_reply(getattr(msg, "tool_calls", None)),
                finish_reason=getattr(choice, "finish_reason", None),
            )
        except Exception as e:
            last_err = e
            wait = 2 ** attempt
            print(f"调用失败（第 {attempt + 1} 次）：{e}，{wait}s 后重试")
            time.sleep(wait)
    raise RuntimeError(f"模型调用连续失败 {retries} 次") from last_err


class _ToolCallBuf:
    """流式 tool_calls 累积缓冲：按 index 聚合 id / name / arguments 片段。"""

    def __init__(self):
        self._by_index: dict[int, dict[str, Any]] = {}

    def add(self, index: int, delta) -> None:
        slot = self._by_index.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if getattr(delta, "id", None):
            # 用 += 而非 =：正常情况下 id 只在首个分片出现，
            # 但个别兼容层会重复/分片发送，+= 幂等且不会丢字符
            slot["id"] += delta.id
        fn = getattr(delta, "function", None)
        if fn is not None:
            if getattr(fn, "name", None):
                slot["name"] += fn.name
            if getattr(fn, "arguments", None):
                slot["arguments"] += fn.arguments

    def to_tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(slot["id"], slot["name"], slot["arguments"])
            for _, slot in sorted(self._by_index.items())
        ]


def chat_stream(
    messages: list,
    *,
    tools=None,
    thinking=True,
    max_tokens=config.LLM_MAX_TOKENS,
    retries=config.LLM_RETRIES,
    echo=True,
) -> Reply:
    """流式调用：逐 token 打印正文，累积 tool_calls，返回统一 Reply。

    echo=True 时把正文增量实时打印到 stdout（end="", flush=True）。
    """
    messages = _inject_date(messages)
    kwargs = _build_kwargs(messages, tools, thinking, max_tokens)
    kwargs["stream"] = True

    last_err = None
    echoed_len = 0  # 已打印到屏幕的字符数：重试时跳过这部分，避免重复打印
    for attempt in range(retries):
        content_parts: list[str] = []
        tool_buf = _ToolCallBuf()
        finish_reason: str | None = None
        try:
            stream = _get_client().chat.completions.create(**kwargs)
            for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                # 记录结束原因（stop / length / tool_calls 等）
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                # 正文增量
                piece = getattr(delta, "content", None)
                if piece:
                    content_parts.append(piece)
                    if echo:
                        print(piece, end="", flush=True)
                        echoed_len += len(piece)
                # 工具调用增量
                d_tool_calls = getattr(delta, "tool_calls", None)
                if d_tool_calls:
                    for tc in d_tool_calls:
                        idx = getattr(tc, "index", 0) or 0
                        tool_buf.add(idx, tc)
            full_content = "".join(content_parts)
            if echo:
                # 重试场景：本次重新生成的内容里，前 echoed_len 字符此前已打印过，
                # 这里只补打增量部分（正常情况下 echoed_len == len(full_content)，无额外输出）
                if len(full_content) > echoed_len:
                    print(full_content[echoed_len:], end="", flush=True)
                print("", flush=True)  # 正文结束后换行
            return Reply(full_content, tool_buf.to_tool_calls(), finish_reason)
        except Exception as e:
            last_err = e
            wait = 2 ** attempt
            print(f"\n流式调用失败（第 {attempt + 1} 次）：{e}，{wait}s 后重新生成（已输出内容不会重复打印）")
            time.sleep(wait)
    raise RuntimeError(f"模型流式调用连续失败 {retries} 次") from last_err
