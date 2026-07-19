# -*- coding: utf-8 -*-
"""
JSON 解析与计划归一化工具（复用自原 Demo，并补充 parse_json_object）。
"""

import json
import re


def _extract_json(text: str, container_re: str, kind: type) -> list | dict | None:
    """健壮地从模型输出里抠出 JSON 容器（数组或对象）的公共形状。

    模型即使被要求「只输出 JSON」，也常包上 ```json 围栏或加一句废话。
    直接 json.loads 会在这里翻车，故依次：剥围栏 → 直接 loads → 正则抠容器再 loads。
    """
    if not text:
        return None
    # 1. 剥离 markdown 围栏
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    # 2. 直接试
    try:
        data = json.loads(text)
        return data if isinstance(data, kind) else None
    except json.JSONDecodeError:
        pass
    # 3. 正则抠出第一个容器块（[...] 或 {...}）
    match = re.search(container_re, text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return data if isinstance(data, kind) else None
        except json.JSONDecodeError:
            pass
    return None


def parse_json_array(text: str) -> list | None:
    """健壮地从模型输出里抠出 JSON 数组。"""
    return _extract_json(text, r"\[.*\]", list)  # type: ignore[return-value]


def parse_json_object(text: str) -> dict | None:
    """健壮地从模型输出里抠出 JSON 对象（用于路由分类结果）。"""
    return _extract_json(text, r"\{.*\}", dict)  # type: ignore[return-value]


def normalize_plan(raw: list | None) -> list[str]:
    """把计划统一归一化成 list[str]。

    模型可能返回 ["调研三元锂"] 也可能返回 [{"subtask": "调研三元锂"}]，
    在入口处一次性抹平，下游就不用到处写 isinstance 了。
    """
    if not raw:
        return []

    plan: list[str] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(
                item.get("subtask") or item.get("task") or item.get("description") or ""
            ).strip()
        else:
            text = str(item).strip()
        if text:
            plan.append(text)
    return plan


def plans_differ(a: list[str], b: list[str]) -> bool:
    """语义级比较，而不是逐字 !=。

    LLM 重述时标点、空格、措辞都会变，逐字比较等于每轮都判定"计划已调整"。
    """
    if len(a) != len(b):
        return True

    def norm(s: str) -> str:
        return re.sub(r"[\s，。、,.\-—:：]", "", s).lower()

    return [norm(x) for x in a] != [norm(x) for x in b]
