# -*- coding: utf-8 -*-
"""
规划器真实 API 冒烟测试（省 token）。

只跑 agent.planner.make_dag_plan 一次，验证 DeepSeek 连通 + 规划器能把任务
拆成合法的 DAG 节点清单（NDJSON/JSON 解析、id/depends_on 归一化成功）。
跑通这个，再去跑完整 REPL 流程，避免一次性烧 token。

运行：
    python test_planner_smoke.py
"""
# agent 包 import 时由 agent.config 统一处理 stdout/stdin 编码，无需手动 reconfigure。
from agent.planner import make_dag_plan

if __name__ == "__main__":
    task = "调研今年新能源汽车电池技术的最新进展，写一份两千字的报告"
    print(f"任务：{task}\n")

    nodes = make_dag_plan(task)

    print(f"规划器产出 {len(nodes)} 个节点：")
    for n in nodes:
        deps = f"  ⇐ 依赖 {n['depends_on']}" if n["depends_on"] else ""
        print(f"  [{n['id']}] {n['subtask']}{deps}")

    # 断言：非空节点清单，每项是含 subtask 的 dict
    assert isinstance(nodes, list) and len(nodes) > 0, "规划器未产出任何节点"
    assert all(isinstance(n, dict) and n.get("subtask", "").strip() for n in nodes), "存在空白节点"
    print("\n[OK] 规划器冒烟验证通过")
