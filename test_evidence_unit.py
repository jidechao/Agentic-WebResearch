# -*- coding: utf-8 -*-
"""证据池（evidence）离线单测（不打真实 API、不触网络）。

覆盖规格 docs/specs/citation.md 的纯逻辑分支：
  - URL 规范化去重（同 URL 不同 tracking 参数 → 同 ID）
  - ID 全局递增 + reset 重开
  - reset_evidence(loaded) 带历史数据回填后 counter 起点正确
  - finalize_report 重映射连续性（[s7][s2][s7] → [1][2][1] + 末尾两条）
  - 幻影引用删除（池里没有的 [sN] 从正文删、不进列表）
  - 未被引用的池条目不进参考列表

运行：python test_evidence_unit.py
"""
from agent import config  # 触发 config 顶部 UTF-8 reconfigure（与 test_dag_unit 同模式）
from agent import evidence


def _build_pool():
    """构造一个已知池：s1 / s2 / s7 三条，counter 起点随之设为 8。

    用 reset_evidence(loaded=...) 直接回填指定 id（模拟落盘恢复），
    使 s7 真实存在于池中——finalize 系列用例据此验证重映射/幻影/未引用。
    """
    evidence.reset_evidence(loaded=[
        {"id": "s1", "title": "A", "url": "https://a.com/1", "date": "2025-01-01"},
        {"id": "s2", "title": "B", "url": "https://b.com/2", "date": "2025-02-01"},
        {"id": "s7", "title": "G", "url": "https://g.com/7", "date": "2025-07-01"},
    ])


def test_url_normalization_dedup():
    # 同一 URL：原始 / 带 utm tracking / 带 fragment → 规范化后完全一致 → 同 ID
    base = "https://example.com/page?id=5"
    evidence.reset_evidence()
    first = evidence.add_evidence(title="X", url=base, date="2025-03-01")
    second = evidence.add_evidence(title="X", url=base + "&utm_source=newsletter", date="2025-03-01")
    third = evidence.add_evidence(title="X", url=base + "#section", date="2025-03-01")
    assert first == second == third, f"去重失效: {first} {second} {third}"
    # 不同 host 不去重（即便语义同源）
    other = evidence.add_evidence(title="Y", url="https://other.org/page?id=5", date="2025-03-01")
    assert other != first, f"不同 URL 被误去重: {other} == {first}"
    print("✓ URL 规范化去重（utm/fragment 不致新 ID；不同 URL 各自入池）:", first, other)


def test_global_id_monotonic():
    evidence.reset_evidence()
    ids = [
        evidence.add_evidence(title=f"T{i}", url=f"https://x.com/{i}", date="2025-01-01")
        for i in range(3)
    ]
    assert ids == ["s1", "s2", "s3"], ids
    # reset 后从 s1 重开（池清空、counter 归零）
    evidence.reset_evidence()
    again = evidence.add_evidence(title="Z", url="https://z.com", date="2025-01-01")
    assert again == "s1", f"reset 后未从 s1 起步: {again}"
    print("✓ ID 全局递增 + reset 重开:", ids, "→ reset →", again)


def test_reset_with_loaded():
    # 模拟 DAG /resume：从落盘文件回填池后，新分配的 ID 不得与历史碰撞
    loaded = [
        {"id": "s1", "title": "旧1", "url": "https://old.com/1", "date": "2024-01-01"},
        {"id": "s2", "title": "旧2", "url": "https://old.com/2", "date": "2024-01-02"},
        {"id": "s3", "title": "旧3", "url": "https://old.com/3", "date": "2024-01-03"},
    ]
    evidence.reset_evidence(loaded=loaded)
    new_id = evidence.add_evidence(title="新", url="https://new.com", date="2025-01-01")
    assert new_id == "s4", f"带历史 reset 后起点应为 s4（len+1）, 实际 {new_id}"
    # 历史条目仍可查到（回填有效）
    pool = evidence.get_pool()
    assert len(pool) == 4 and pool[0]["id"] == "s1", pool
    print("✓ reset_evidence(loaded) 回填 + counter 起点 len+1:", new_id, "池大小", len(pool))


def test_finalize_remap_continuous():
    _build_pool()  # s1 / s2 / s7
    content = "第一处引[s7]中间引[s2]再引[s7]收尾。"
    out = evidence.finalize_report(content)
    # 首次出现顺序：s7 → 1, s2 → 2；重复出现的 s7 仍是 1
    assert "[1]" in out and "[2]" in out, out
    # 出现顺序：s7 先（=1），s2 后（=2）
    pos1 = out.index("[1]")
    pos2 = out.index("[2]")
    assert pos1 < pos2, f"展示编号应按正文首次出现顺序: {pos1} vs {pos2}"
    # 参考列表应含且仅含 2 条（s7 与 s2 被引用；s1 未被引用）
    assert "[1] [G](https://g.com/7)" in out, out
    assert "[2] [B](https://b.com/2)" in out, out
    # 参考章节标题存在
    assert "参考" in out, out
    print("✓ finalize_report 重映射连续 + 末尾参考列表（按首次出现排序）")


def test_finalize_phantom_deleted():
    _build_pool()  # 池里只有 s1/s2/s7
    content = "正文引真实[s1]和幻影[s99]两处。"
    out = evidence.finalize_report(content)
    # 幻影 s99：正文删除、不进参考列表
    assert "[s99]" not in out, f"幻影引用未删除: {out}"
    assert "99" not in out, f"幻影编号泄漏进参考列表: {out}"
    # 真实 s1 保留并重映射为 [1]
    assert "[1]" in out, out
    assert "[1] [A](https://a.com/1)" in out, out
    print("✓ 幻影引用删除（池里没有的 [sN] 不进正文/列表）")


def test_finalize_preserves_unused():
    _build_pool()  # s1/s2/s7 都在池里
    content = "正文只引[s2]一条。"
    out = evidence.finalize_report(content)
    # 被引用的 s2 → [1]，进列表
    assert "[1]" in out and "[1] [B](https://b.com/2)" in out, out
    # 未被引用的 s1 / s7 不进参考列表（诚实降级，不罗列全部搜索结果）
    assert "a.com/1" not in out, f"未引用条目泄漏进列表: {out}"
    assert "g.com/7" not in out, f"未引用条目泄漏进列表: {out}"
    print("✓ 未被引用的池条目不进参考列表")


def test_finalize_no_citations_honest_degradation():
    # 无标记兜底：模型全程未标 [sN] → 报告无参考列表（诚实降级，不补造）
    _build_pool()
    content = "这段正文没有任何引用标记。"
    out = evidence.finalize_report(content)
    assert out.strip() == content.strip(), f"无引用时正文应原样返回: {out!r}"
    assert "参考" not in out, f"无引用时不应拼参考章节: {out!r}"
    print("✓ 无标记兜底：报告无参考列表，诚实降级")


if __name__ == "__main__":
    test_url_normalization_dedup()
    test_global_id_monotonic()
    test_reset_with_loaded()
    test_finalize_remap_continuous()
    test_finalize_phantom_deleted()
    test_finalize_preserves_unused()
    test_finalize_no_citations_honest_degradation()
    print("\nALL EVIDENCE UNIT TESTS PASSED")