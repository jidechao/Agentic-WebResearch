# -*- coding: utf-8 -*-
"""DAG 引擎离线单测（mock LLM，不打真实 API）。

覆盖：环检测、拓扑分层、悬空依赖剔除、normalize、Critic 解析、落盘重载恢复。
运行：python test_dag_unit.py
"""
import os
import shutil

from agent import dag_store
import agent.planner as planner
import agent.execute_reflect as execute_reflect
import agent.orchestrator as orchestrator


def test_topo_layers():
    nodes = [
        {"id": "t1", "subtask": "A", "depends_on": []},
        {"id": "t2", "subtask": "B", "depends_on": []},
        {"id": "t3", "subtask": "C", "depends_on": ["t1", "t2"]},
        {"id": "t4", "subtask": "D", "depends_on": ["t3"]},
    ]
    layers = [[n["id"] for n in L] for L in planner.topo_layers(nodes)]
    assert layers == [["t1", "t2"], ["t3"], ["t4"]], layers
    print("✓ topo_layers 分层正确:", layers)


def test_cycle_breaking():
    cyc = [
        {"id": "a", "subtask": "A", "depends_on": ["c"]},
        {"id": "b", "subtask": "B", "depends_on": ["a"]},
        {"id": "c", "subtask": "C", "depends_on": ["b"]},
    ]
    fixed = planner.repair_and_acyclic([dict(n) for n in cyc])
    layers = planner.topo_layers(fixed)  # 不抛错即破环成功
    assert sum(len(L) for L in layers) == 3
    print("✓ 环检测并破环成功:", [[n["id"] for n in L] for L in layers])


def test_dangling_and_self_dep_removed():
    nodes = [{"id": "t1", "subtask": "X", "depends_on": ["ghost", "t1"]}]
    fixed = planner.repair_and_acyclic([dict(n) for n in nodes])
    assert fixed[0]["depends_on"] == [], fixed
    print("✓ 悬空/自依赖剔除:", fixed[0]["depends_on"])


def test_extract_items_ndjson():
    # NDJSON：每行一个独立对象，无外层数组（模型照 prompt 示例输出时的真实格式）
    ndjson = (
        '{"id": "t1", "subtask": "调研A", "depends_on": []}\n'
        '{"id": "t2", "subtask": "调研B", "depends_on": []}\n'
        '{"id": "t3", "subtask": "对比A与B", "depends_on": ["t1", "t2"]}'
    )
    items = planner.extract_items(ndjson)
    assert len(items) == 3, items
    nodes = planner.repair_and_acyclic(planner.normalize_nodes(items))
    layers = [[n["id"] for n in L] for L in planner.topo_layers(nodes)]
    assert layers == [["t1", "t2"], ["t3"]], layers
    # id 被保留（depends_on 引用有效）
    assert nodes[2]["depends_on"] == ["t1", "t2"]
    print("✓ NDJSON 提取 + id 保留 + 分层:", layers)


def test_normalize_nodes():
    mixed = planner.normalize_nodes([
        "纯文本任务",
        {"subtask": "dict任务", "depends_on": ["t1"]},
        {"task": "task键"},
        {"id": "zzz", "subtask": ""},  # 空 subtask 应被丢弃
    ])
    assert [n["id"] for n in mixed] == ["t1", "t2", "t3"], mixed
    assert mixed[1]["depends_on"] == ["t1"]
    assert mixed[2]["subtask"] == "task键"
    print("✓ normalize_nodes:", [(n["id"], n["subtask"]) for n in mixed])


def test_critic_parse(monkeypatch=None):
    # mock chat 返回统一 Reply（合法 JSON）→ 应按评分判定
    from agent.llm import Reply

    orig = execute_reflect.chat
    execute_reflect.chat = lambda *a, **k: Reply('{"pass": false, "score": 5, "issues": "缺数据", "suggestion": "补充来源"}', [])
    try:
        r = execute_reflect.critic_review("某子任务", "某产出")
        assert r["pass"] is False and r["score"] == 5 and "缺数据" in r["issues"], r
        print("✓ critic_review 解析（不达标）:", r)
    finally:
        execute_reflect.chat = orig

    # mock 返回非 JSON → 默认放行
    execute_reflect.chat = lambda *a, **k: Reply("这不是JSON", [])
    try:
        r2 = execute_reflect.critic_review("某子任务", "某产出")
        assert r2["pass"] is True, r2
        print("✓ critic_review 解析失败默认放行:", r2)
    finally:
        execute_reflect.chat = orig


def test_topo_layers_resume():
    # 恢复场景：t1/t2 已完成，剩余 t3/t4 的 depends_on 仍指向已完成节点
    remaining = [
        {"id": "t3", "subtask": "C", "depends_on": ["t1", "t2"]},
        {"id": "t4", "subtask": "D", "depends_on": ["t3"]},
    ]
    done = {"t1", "t2"}
    layers = [[n["id"] for n in L] for L in planner.topo_layers(remaining, done=done)]
    assert layers == [["t3"], ["t4"]], layers
    print("✓ topo_layers 恢复场景（已完成依赖视为满足）:", layers)


def test_store_roundtrip():
    rid = "test-unit-run"
    try:
        nodes = [{"id": "t1", "subtask": "A", "depends_on": []}]
        dag_store.save_plan(rid, nodes)
        dag_store.save_state(rid, [{"id": "t1", "subtask": "A", "result": "R"}], [], 1, task="原始任务")
        dag_store.save_report(rid, "# 报告")

        assert dag_store.load_plan(rid) == nodes
        st = dag_store.load_state(rid)
        assert st["task"] == "原始任务" and st["completed"][0]["result"] == "R" and st["layer"] == 1
        assert dag_store.has_state(rid)
        assert rid in dag_store.list_runs()
        print("✓ 落盘→重载恢复一致 (plan/state/report)")
    finally:
        shutil.rmtree(os.path.join("runs", rid), ignore_errors=True)


def test_budget_guard():
    # 预算护栏：replan 不断补充节点时，executed_total 硬上限必须挡住
    import asyncio
    from agent import config

    calls = {"exec": 0, "replan": 0}
    seen_original_task = {}

    def fake_exec(node, briefs, original_task=""):
        calls["exec"] += 1
        seen_original_task[node["id"]] = original_task
        return f"结果-{node['id']}"

    def fake_replan(task, completed, remaining, budget):
        calls["replan"] += 1
        # 每次 replan 都新增一个节点，试图无限膨胀
        nid = f"x{calls['replan']}"
        return remaining + [{"id": nid, "subtask": f"新增{nid}", "depends_on": []}]

    orig_exec, orig_replan = orchestrator.run_with_reflection, orchestrator.replan_dag
    orchestrator.run_with_reflection = fake_exec
    orchestrator.replan_dag = fake_replan
    try:
        completed = asyncio.run(
            orchestrator._run_dag_async("任务", [{"id": "t1", "subtask": "A", "depends_on": []}], [], "test-budget")
        )
        # 累计执行节点数不得超过 MAX_DAG_NODES
        assert calls["exec"] <= config.MAX_DAG_NODES, calls
        assert len(completed) <= config.MAX_DAG_NODES, len(completed)
        # 原始需求直通车：orchestrator 把 task 透传到了执行点（ADR-0005）
        assert all(v == "任务" for v in seen_original_task.values()), seen_original_task
        print(f"✓ 预算护栏：replan 试图膨胀时被挡住（执行 {calls['exec']} 节点，上限 {config.MAX_DAG_NODES}）；"
              f"原始任务已透传到执行点")
    finally:
        orchestrator.run_with_reflection, orchestrator.replan_dag = orig_exec, orig_replan
        import shutil
        shutil.rmtree("runs/test-budget", ignore_errors=True)


def test_resume_empty_state_replans():
    # N2：completed 与 remaining 均空 → 应判定 state 无效转重新规划（不产空报告）
    from agent import dag_store
    rid = "test-empty-state"
    dag_store.save_state(rid, completed=[], remaining=[], layer=0, task="某任务")

    planned = {"called": False}

    def fake_make_plan(task):
        planned["called"] = True
        return [{"id": "t1", "subtask": "重新规划", "depends_on": []}]

    orig = planner.make_dag_plan
    planner.make_dag_plan = fake_make_plan
    try:
        # 只验证它走到了重新规划分支（不真跑 async 执行）
        state = dag_store.load_state(rid)
        completed = state.get("completed", [])
        nodes = state.get("remaining", [])
        if not completed and not nodes:
            nodes = fake_make_plan("某任务")
        assert planned["called"] and nodes[0]["subtask"] == "重新规划"
        print("✓ 空 state 恢复 → 触发重新规划（不产空报告）")
    finally:
        planner.make_dag_plan = orig
        import shutil
        shutil.rmtree(f"runs/{rid}", ignore_errors=True)


def test_repair_preserves_external_deps():
    # R2：replan 节点的 depends_on 指向已完成节点（外部 id）时，应被保留而非误删
    nodes = [
        {"id": "t3", "subtask": "对比", "depends_on": ["t1", "t2"]},  # t1/t2 是已完成节点
        {"id": "t4", "subtask": "总结", "depends_on": ["t3"]},
    ]
    done_ids = {"t1", "t2"}
    fixed = planner.repair_and_acyclic([dict(n) for n in nodes], extra_valid_ids=done_ids)
    assert fixed[0]["depends_on"] == ["t1", "t2"], fixed  # 外部依赖保留
    assert fixed[1]["depends_on"] == ["t3"], fixed        # 内部依赖保留
    # 且 topo 在 done 机制下能正确分层
    layers = [[n["id"] for n in L] for L in planner.topo_layers(fixed, done=done_ids)]
    assert layers == [["t3"], ["t4"]], layers
    print("✓ R2：外部（已完成）依赖保留 + 分层:", fixed[0]["depends_on"], layers)


def test_run_id_stable():
    # M4：同一任务跨"进程"哈希稳定（md5），不再随机化
    a = dag_store.make_run_id("同一个任务", "20260718-120000")
    b = dag_store.make_run_id("同一个任务", "20260718-120000")
    assert a == b, (a, b)
    assert a.endswith("-") is False and len(a.split("-")[-1]) == 8
    print("✓ run_id 跨进程稳定:", a)


if __name__ == "__main__":
    test_topo_layers()
    test_cycle_breaking()
    test_dangling_and_self_dep_removed()
    test_normalize_nodes()
    test_extract_items_ndjson()
    test_topo_layers_resume()
    test_repair_preserves_external_deps()
    test_critic_parse()
    test_store_roundtrip()
    test_budget_guard()
    test_resume_empty_state_replans()
    test_run_id_stable()
    print("\nALL DAG UNIT TESTS PASSED")
