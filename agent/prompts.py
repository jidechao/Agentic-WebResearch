# -*- coding: utf-8 -*-
"""
共享提示词：被多个引擎复用的 prompt 文本。

单处定义，消除 plan_execute（线性引擎）与 execute_reflect / orchestrator
（DAG 引擎）之间的复制。各引擎独有的 prompt（规划 / 重规划 / critic）留在各自模块。
"""

import json

# 子任务执行的系统提示：线性引擎（plan_execute）与 DAG 引擎（execute_reflect）共用。
SUBTASK_SYSTEM = (
    "你是一个调研助手。针对当前子任务进行联网搜索并总结要点。\n"
    "要求：\n"
    "1. 优先使用 web_search 工具获取真实信息，不要凭记忆编造；\n"
    "2. 总结时保留关键数据、时间和来源；\n"
    "3. 引用证据时必须使用搜索结果里出现的 [sN] 标记，不要自编编号；\n"
    "4. 信息足够后直接给出总结，不要过度搜索。"
)


def summary_prompt(task: str, items: list[dict], feedback: str = "") -> str:
    """构造最终报告的汇总提示。

    items: 各子任务的 {subtask, result} 列表（线性引擎直接传 completed，
    DAG 引擎传由 completed 裁出的 brief）。
    feedback: 报告级 Reviewer 打回时的改进意见（issues + suggestion）；非空时追加
    "上次报告的问题"段，驱动重写。
    """
    feedback_section = (
        f"\n\n## 上次报告的问题（请务必修正）\n{feedback}" if feedback else ""
    )
    return f"""任务：{task}
以下是各子任务的调研结果：
{json.dumps(items, ensure_ascii=False, indent=2)}
请基于以上信息，撰写一份结构完整、逻辑清晰的报告。要求：
1. 有清晰的章节结构；
2. 保留调研结果中的关键数据和时间；
3. 只使用上述调研结果中出现的信息，不要编造；
4. 务必覆盖原始需求的每条约束——对比/归纳类要求要真做对照，而非把素材罗列一遍；
5. 引用证据时在断言处内联标注搜索结果里出现的 [sN] 标记，不要在末尾单独罗列"参考来源""参考文献"等编号段——系统会自动生成完整参考列表，自列会与之重复；不要自编编号；
6. 篇幅约 2000 字。{feedback_section}"""