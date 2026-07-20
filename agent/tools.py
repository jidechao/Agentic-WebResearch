# -*- coding: utf-8 -*-
"""
联网搜索工具（博查 Web Search API）+ 其 function-calling schema。
"""

import json

import requests

from . import config
from . import evidence


def web_search(query: str) -> str:
    """博查 Web Search API —— DeepSeek 官方的联网搜索供应方，国内直连。"""
    if not query or not query.strip():
        return "搜索失败：查询词为空。"
    if not config.BOCHA_API_KEY:
        return "搜索失败：未配置 BOCHA_API_KEY（请在 .env 中填入博查密钥）。"

    payload = {
        "query": query,
        "freshness": config.SEARCH_FRESHNESS,          # 调研类任务限定近一年，"noLimit" 覆盖更广
        "summary": True,                 # 返回长摘要而非一句话 snippet
        "count": config.MAX_SEARCH_RESULTS,
    }
    headers = {
        "Authorization": f"Bearer {config.BOCHA_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(config.BOCHA_URL, headers=headers, json=payload, timeout=config.SEARCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"搜索失败：{e}"
    except json.JSONDecodeError:
        return "搜索失败：返回内容不是合法 JSON。"

    # 防御：API 可能返回 "data": null 或缺层，避免在 None 上 .get 崩溃
    pages = (
        (data.get("data") or {})
        .get("webPages") or {}
    ).get("value", [])
    if not pages:
        return f"未搜索到与「{query}」相关的信息。"

    lines = []
    for idx, page in enumerate(pages, start=1):
        title = page.get("name", "")
        url = page.get("url", "")
        # summary 是长摘要，snippet 是短摘要，优先长的
        content = (page.get("summary") or page.get("snippet") or "").strip()
        date = page.get("datePublished") or page.get("dateLastCrawled") or ""
        # 截断，防止单条结果撑爆上下文
        if len(content) > config.SEARCH_CONTENT_LEN:
            content = content[: config.SEARCH_CONTENT_LEN] + "..."
        lines.append(
            # 锚定在工具层：解析博查返回时即调 add_evidence 分配运行期 ID [sN]，
            # 模型在 ReAct 消息里只能复制见过的标记，无法注入自编 URL（ADR-0009）。
            # title/url/date 直取 API（零幻觉），不存摘要正文（池只保留溯源必需字段）。
            f"[{evidence.add_evidence(title=title, url=url, date=date[:10])}] {title}\n"
            f"    时间：{date[:10]}\n"
            f"    来源：{url}\n"
            f"    摘要：{content}"
        )
    return "\n\n".join(lines)


SEARCH_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索实时信息。适用于查询最新进展、行业数据、企业动态等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，简洁精准，5-15 字为宜。",
                    }
                },
                "required": ["query"],
            },
        },
    }
]


def web_search_tool(name: str, arguments: dict) -> str:
    """web_search 的工具协议适配器（execute_tool 回调）。

    薄薄一层 glue：把 LLM 的工具协议（name + arguments dict）翻译成
    web_search 能懂的调用。web_search 本身保持单一职责（只懂搜索），
    不被通用工具协议污染。将来加新工具时，各自有各自的适配器。
    """
    query = arguments.get("query", "")
    print(f"   🔍 搜索：{query}")
    return web_search(query)
