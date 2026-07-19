# -*- coding: utf-8 -*-
"""对修复点的离线单元自检（不打真实 API，除 web_search 用假响应 mock）。"""
import json

import agent.execute_reflect as ER
import agent.planner as P
import agent.llm
import agent.tools as T
from agent import config
from agent.llm import Reply, _ToolCallBuf


class Fn:
    def __init__(self, name=None, args=None):
        self.name = name
        self.arguments = args


class D:
    def __init__(self, id=None, fn=None):
        self.id = id
        self.function = fn


def test_h1_toolcall_id_accumulation():
    buf = _ToolCallBuf()
    buf.add(0, D(id="call_"))
    buf.add(0, D(fn=Fn(name="web_")))
    buf.add(0, D(id="abc", fn=Fn(name="search", args='{"que')))
    buf.add(0, D(fn=Fn(args='ry": "x"}')))
    tc = buf.to_tool_calls()[0]
    assert tc.id == "call_abc", f"id 累积错误: {tc.id}"
    assert tc.name == "web_search", f"name 累积错误: {tc.name}"
    assert json.loads(tc.raw_arguments) == {"query": "x"}, f"args 累积错误: {tc.raw_arguments}"
    print("H1 OK: id/name/args 分片累积正确 ->", tc.id, tc.name, tc.raw_arguments)


def test_m3_finish_reason():
    m = Reply("正文", [], "length")
    assert m.finish_reason == "length" and m.content == "正文"
    print("M3 OK: Reply.finish_reason =", m.finish_reason)


def test_m2_web_search_null_data():
    import requests

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": None}

    orig = requests.post
    requests.post = lambda *a, **k: FakeResp()
    try:
        out = T.web_search("测试")
        assert "未搜索到" in out, f"预期未搜索到提示, 实际: {out}"
        print("M2 OK: data=null 时不崩 ->", out)
    finally:
        requests.post = orig


def test_passthrough_critic_origin_injection():
    """Critic 收到原始需求：monkeypatch chat，断言 prompt 含原文 + '原始需求'标记。"""
    captured = {}

    def fake_chat(messages, thinking=False):
        captured["prompt"] = messages[0]["content"]
        return Reply('{"pass": true, "score": 8, "issues": "", "suggestion": ""}', [], "stop")

    orig = ER.chat
    ER.chat = fake_chat
    try:
        ER.critic_review("对比 A 与 B", "结果内容", original_task="原文重点看成本与能量密度")
    finally:
        ER.chat = orig
    assert "原文重点看成本与能量密度" in captured["prompt"], "原始任务未进入 critic prompt"
    assert "原始需求" in captured["prompt"], "critic prompt 缺'原始需求'标记"
    assert "代言人" in captured["prompt"], "critic prompt 缺'用户代言人'对齐语"
    print("P1 OK: 原始需求已注入 critic prompt（含代言人对齐）")


def test_passthrough_executor_origin_injection():
    """Executor 收到原始需求：monkeypatch react_loop，断言 user_content 含原文。"""
    captured = {}

    def fake_react_loop(user_content, **kw):
        captured["user_content"] = user_content
        return Reply("执行结果", [], "stop")

    orig = ER.react_loop
    ER.react_loop = fake_react_loop
    try:
        ER.execute_subtask("子任务描述", [], original_task="原文YYY必须满足")
    finally:
        ER.react_loop = orig
    assert "原文YYY必须满足" in captured["user_content"], "原始任务未进入 executor user content"
    assert "用户原始需求" in captured["user_content"], "executor user content 缺'用户原始需求'段"
    print("P2 OK: 原始需求已前置进入 executor user content")


def test_passthrough_empty_no_injection():
    """original_task 缺省时不注入：保降级/兼容路径行为不变。"""
    cap_c = {}

    def fake_chat(messages, thinking=False):
        cap_c["p"] = messages[0]["content"]
        return Reply('{"pass": true, "score": 8, "issues": "", "suggestion": ""}', [], "stop")

    cap_e = {}

    def fake_react_loop(user_content, **kw):
        cap_e["u"] = user_content
        return Reply("x", [], "stop")

    oc, ore = ER.chat, ER.react_loop
    ER.chat, ER.react_loop = fake_chat, fake_react_loop
    try:
        ER.critic_review("子任务", "结果")          # original_task 取默认 ""
        ER.execute_subtask("子任务", [])            # original_task 取默认 ""
    finally:
        ER.chat, ER.react_loop = oc, ore
    assert "原始需求" not in cap_c["p"], "空 original_task 时 critic 不应注入原始需求段"
    assert "用户原始需求" not in cap_e["u"], "空 original_task 时 executor 不应注入原始需求段"
    print("P3 OK: original_task 缺省时不注入（降级路径行为不变）")


def test_report_review_injection_and_parse():
    """report_review：prompt 含 task/completed/report；missing_constraints 解析正确。"""
    captured = {}

    def fake_chat(messages, thinking=False):
        captured["prompt"] = messages[0]["content"]
        return Reply(
            '{"pass": false, "score": 5, "issues": "漏了量产时间表", '
            '"suggestion": "补一节", "missing_constraints": ["量产时间表"]}',
            [], "stop",
        )

    orig = ER.chat
    ER.chat = fake_chat
    try:
        r = ER.report_review(
            "调研电池进展并单独列出量产时间表",
            "# 报告\n...（无时间表）",
            [{"subtask": "调研固态电池", "result": "进展..."}],
        )
    finally:
        ER.chat = orig
    assert "调研电池进展并单独列出量产时间表" in captured["prompt"], "原始任务未进入 report_review prompt"
    assert "调研固态电池" in captured["prompt"], "completed 素材未进入 prompt"
    assert "# 报告" in captured["prompt"], "报告未进入 prompt"
    assert r["pass"] is False and r["score"] == 5, r
    assert r["missing_constraints"] == ["量产时间表"], r
    print("R1 OK: report_review 注入 task/completed/report + missing_constraints 解析正确")


def test_report_review_parse_fail_passthrough():
    """report_review：chat 返回非 JSON → 默认放行，missing 为空。"""
    orig = ER.chat
    ER.chat = lambda *a, **k: Reply("这不是JSON", [])
    try:
        r = ER.report_review("任务", "报告", [{"subtask": "x", "result": "y"}])
    finally:
        ER.chat = orig
    assert r["pass"] is True and r["missing_constraints"] == [], r
    print("R2 OK: report_review 解析失败默认放行（missing 空）")


def test_plan_missing_budget_and_ids():
    """plan_missing：预算>0 产 m* 独立节点；预算耗尽返回空。"""
    orig = P.chat
    P.chat = lambda *a, **k: Reply(
        '[{"id":"x1","subtask":"查量产表","depends_on":["t1"]}, {"subtask":"查成本"}]', []
    )
    try:
        nodes = P.plan_missing("任务", ["量产时间表"], [{"id": "t1", "subtask": "a", "result": "r"}])
        # 预算耗尽：completed 已达 MAX_DAG_NODES（budget<=0 提前返回，不调 chat）
        full = [{"id": f"t{i}", "subtask": "x", "result": "r"} for i in range(config.MAX_DAG_NODES)]
        empty = P.plan_missing("任务", ["缺口"], full)
    finally:
        P.chat = orig
    assert len(nodes) == 2, nodes
    assert [n["id"] for n in nodes] == ["m1", "m2"], "id 应重打为 m*（防与 t* 碰撞）"
    assert all(n["depends_on"] == [] for n in nodes), "补研节点应强制独立"
    assert empty == [], "预算耗尽应返回空列表"
    print("R3 OK: plan_missing 预算守卫 + m* id 重打 + 强制独立")


def test_llm_date_injection():
    """集中日期注入：已有 system 合并；无 system 插入；日期串含年月日+星期。"""
    import re
    import agent.llm as L

    # 已有 system → 合并进去（不产生双 system 消息）
    a = L._inject_date([{"role": "system", "content": "你是助手。"}])
    assert len(a) == 1 and a[0]["role"] == "system", a
    assert a[0]["content"].startswith("当前日期："), a[0]["content"][:10]
    assert "你是助手。" in a[0]["content"], "原 system 内容必须保留"

    # 无 system → 在头部插入一个 system 消息
    b = L._inject_date([{"role": "user", "content": "问"}])
    assert len(b) == 2 and b[0]["role"] == "system" and b[1]["role"] == "user", b

    # 日期串格式（年份/月/日 + 星期）
    line = L._today_line()
    assert re.search(r"\d{4}年\d{1,2}月\d{1,2}日", line), line
    assert "星期" in line, line
    print("D1 OK: 日期集中注入（合并/插入/格式正确）→", line)


def test_react_prints_when_stream_off():
    """run_react：stream=False 补打答案；stream=True 不重复打印（靠 chat_stream echo）。"""
    import contextlib
    import io

    import agent.react as R

    def fake_react_loop(user_content, **kw):
        return Reply("这是最终答案。", [], "stop")

    orig = R.react_loop
    R.react_loop = fake_react_loop
    try:
        # 非流式：应 print 答案
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = R.run_react("随便问", stream=False)
        assert out == "这是最终答案。", out
        assert "这是最终答案。" in buf.getvalue(), "stream=False 时应 print 答案"

        # 流式：不应再 print（避免与 chat_stream echo 重复）
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            R.run_react("随便问", stream=True)
        assert "这是最终答案。" not in buf2.getvalue(), "stream=True 不应再 print"
    finally:
        R.react_loop = orig
    print("RP1 OK: run_react 非流式补打答案 / 流式不重复打印")


def test_config_toml_loader():
    """config.toml 加载器：无文件→{} / 合法→dict / 损坏→{}；_o_* 覆盖/缺失/非法兜底。"""
    import os
    import tempfile
    from pathlib import Path
    from agent import config as C

    # 无 config.toml 时（测试环境）内置默认齐全 —— 零回归保证
    assert C.MAX_DAG_NODES == 8 and C.LLM_TIMEOUT == 120.0 and C.SEARCH_FRESHNESS == "oneYear"

    # 无文件 → {}
    assert C._load_overrides(Path("/nonexistent/x.toml")) == {}

    # 合法 toml → dict
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as f:
        f.write('[limits]\ndag_nodes = 3\n[search]\nfreshness = "noLimit"\n')
        tmp = Path(f.name)
    try:
        o = C._load_overrides(tmp)
        assert o["limits"]["dag_nodes"] == 3 and o["search"]["freshness"] == "noLimit"
    finally:
        os.remove(tmp)

    # 损坏 toml → {}（不抛）
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as f:
        f.write("not = valid = toml = =")
        bad = Path(f.name)
    try:
        assert C._load_overrides(bad) == {}
    finally:
        os.remove(bad)

    # _o_* 覆盖 / 缺失 / 非法值
    orig = C._O
    try:
        C._O = {"limits": {"dag_nodes": 3}, "llm": {"timeout": 60.0}, "search": {"freshness": "noLimit"}}
        assert C._o_int("limits", "dag_nodes", 8) == 3       # 覆盖
        assert C._o_int("limits", "plan_steps", 8) == 8      # 键缺失→默认
        assert C._o_float("llm", "timeout", 120.0) == 60.0
        assert C._o_str("search", "freshness", "oneYear") == "noLimit"
        C._O = {"limits": {"dag_nodes": "abc"}}
        assert C._o_int("limits", "dag_nodes", 8) == 8       # 非法→兜底
    finally:
        C._O = orig
    print("T1 OK: config.toml 加载器（无文件/合法/损坏）+ _o_* 覆盖/缺失/非法")


if __name__ == "__main__":
    test_h1_toolcall_id_accumulation()
    test_m3_finish_reason()
    test_m2_web_search_null_data()
    test_passthrough_critic_origin_injection()
    test_passthrough_executor_origin_injection()
    test_passthrough_empty_no_injection()
    test_report_review_injection_and_parse()
    test_report_review_parse_fail_passthrough()
    test_plan_missing_budget_and_ids()
    test_llm_date_injection()
    test_react_prints_when_stream_off()
    test_config_toml_loader()
    print("ALL UNIT CHECKS PASSED")
