# -*- coding: utf-8 -*-
"""
证据池（evidence）：最终报告引用溯源的后端。

职责（单一）：
  - 全局证据池（单 run 作用域）：{id, title, url, date} 条目，append-only。
  - URL 规范化去重：同 URL 只算一条（用户故事 #8）。
  - ID 分配：工具层（web_search 解析博查返回时）调 add_evidence 拿运行期 ID [sN]。
  - 报告后处理：finalize_report 把运行期 [sN] 重映射为连续展示 [1]..[n]，
    删除幻影引用，拼接参考章节。

不依赖 LLM 与网络。引用事实源锚定在工具层（ADR-0009）——参考列表的 URL
由证据池（API 直取）唯一决定，模型无法注入自编 URL。

并发安全：add_evidence 的"查去重表 → 分配 ID → 写入"是 check-then-set 序列，
DAG 引擎同层子任务经 asyncio.to_thread 进真线程池并发调 web_search 时可能
撞同一 URL。用模块级 threading.Lock 包住整个序列，彻底兑现"同 URL 只算一条"。
GIL 只保证单条字节码原子，不保证 check-then-set 序列原子。

形态决策见 docs/specs/citation.md，权衡见 docs/adr/0009-citation-from-tool-layer.md。
"""

import re
import threading
from urllib.parse import urldefrag, urlparse, urlunparse, parse_qsl, urlencode

# ---------------------------------------------------------------------- 模块级状态
# 单 run 作用域：整个任务共享一个池与一个递增计数器。
# 调用方（react / plan_execute / orchestrator 三入口）在新任务发起时 reset_evidence()。
# DAG resume 走 reset_evidence(loaded=load_evidence(run_id)) 回填。
_pool: list[dict] = []
_url_index: dict[str, str] = {}   # 规范化 URL → 已分配的 id（去重表）
_counter: int = 0                  # 下一个分配的数字 ID（= 已分配数 + 1）
_lock = threading.Lock()           # 包住 check-then-set 序列


# ---------------------------------------------------------------------- URL 规范化
def normalize_url(url: str) -> str:
    """规范化 URL 用于去重：去 fragment、去 utm_ 等 tracking 参数、host 小写。

    不同 URL 即便指向同一事实也不去重（不同来源，溯源价值不同）。
    规范化只消除"同源但带跟踪/锚点碎片"的伪差异。
    """
    if not url:
        return ""
    # 去 fragment（#section）
    url, _frag = urldefrag(url)
    parts = urlparse(url)
    # host 小写（大小写不区分），scheme/path 保留原样
    netloc = parts.netloc.lower()
    # 去 tracking 参数（utm_* 一族 + 常见的 gclid/fbclid）
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}
    ]
    query = urlencode(kept)
    return urlunparse(parts._replace(netloc=netloc, query=query))


# ---------------------------------------------------------------------- 核心 API
def reset_evidence(loaded: list[dict] | None = None) -> None:
    """重置证据池。

    loaded=None 或空：清空池，counter 归零（新任务三入口走这条）。
    loaded 非空：回填历史条目（DAG /resume），counter 起点设为 len(loaded)+1，
    保证恢复后新分配的 ID 与历史不碰撞。
    """
    global _pool, _url_index, _counter
    with _lock:
        _pool = list(loaded) if loaded else []
        _url_index = {}
        for item in _pool:
            norm = normalize_url(item.get("url", ""))
            if norm:
                _url_index[norm] = item["id"]
        _counter = len(_pool)


def add_evidence(title: str, url: str, date: str = "") -> str:
    """登记一条证据，返回运行期 ID（"sN"）。

    URL 规范化后查去重表：命中则复用首次分配的 ID（同 URL 只算一条）；
    未命中则在锁内分配新 ID 并写入池。整个 check-then-set 序列由 _lock 保护。
    """
    global _counter
    norm = normalize_url(url)
    with _lock:
        existing = _url_index.get(norm)
        if existing is not None:
            return existing
        _counter += 1
        eid = f"s{_counter}"
        _pool.append({"id": eid, "title": title, "url": url, "date": date or ""})
        if norm:
            _url_index[norm] = eid
        return eid


def get_pool() -> list[dict]:
    """返回池内容的浅拷贝（用于持久化 / 测试）。"""
    with _lock:
        return [dict(item) for item in _pool]


# ---------------------------------------------------------------------- 报告后处理
# 匹配 [sN] 锚点；捕获组 1 = 纯数字（不含 s 前缀），便于重建 id。
_SMARK_RE = re.compile(r"\[s(\d+)\]")


def finalize_report(content: str) -> str:
    """报告后处理：运行期 [sN] → 连续展示 [1]..[n]，删幻影引用，拼参考列表。

    只按正文 [sN] 查池展开——参考列表的 URL 由证据池（API 直取）唯一决定，
    模型无法注入自编 URL（P1 兑现承诺）。池里没有的 [sN]（幻影引用，源于模型
    幻觉）从正文删除，不触发重写（report_review 看到的是原始 [sN]）。

    步骤：
      1. 单遍扫描 [sN] 标记，按首次出现顺序建立 {sid: k} 映射（池里没有的不建映射）。
      2. 删除池中不存在的标记（幻影）。
      3. 正则替换所有 [sN] 为连续 [k]（k 从 1 起）。
      4. 末尾拼接 ## 参考章节，格式「[k] [标题](URL) · 日期」。
      5. 无任何真实引用 → 原样返回（诚实降级，不补造参考列表）。

    执行时机：在报告级反思循环全部结束后、save_report 前执行一次（P2）。
    """
    by_id = {item["id"]: item for item in get_pool()}
    # 1. 按首次出现建立展示编号映射（只对池里存在的标记）
    remap: dict[str, int] = {}
    next_k = 0
    for m in _SMARK_RE.finditer(content):
        sid = "s" + m.group(1)  # 形如 s7
        if sid in by_id and sid not in remap:
            next_k += 1
            remap[sid] = next_k

    # 2. 删除幻影标记（池里没有的）
    def _strip_phantom(m: re.Match) -> str:
        sid = "s" + m.group(1)
        return "" if sid not in by_id else m.group(0)
    content = _SMARK_RE.sub(_strip_phantom, content)

    # 3. 重映射为连续 [k]（按首次出现顺序）
    # 先用占位符避免 [s1]→[1] 后又被后续替换误伤；两遍替换保证幂等。
    def _to_placeholder(m: re.Match) -> str:
        sid = "s" + m.group(1)
        k = remap.get(sid)
        return f"\x00{k}\x00" if k is not None else m.group(0)
    content = _SMARK_RE.sub(_to_placeholder, content)
    content = re.sub(r"\x00(\d+)\x00", lambda m: f"[{m.group(1)}]", content)

    # 4/5. 拼接参考章节（无引用则原样返回）
    if not remap:
        return content
    lines = ["", "## 参考", ""]
    for sid, k in remap.items():
        item = by_id[sid]
        title = item.get("title") or item.get("url") or "（无标题）"
        url = item.get("url", "")
        date = (item.get("date") or "")[:10]
        sep = " · " if date else ""
        lines.append(f"[{k}] [{title}]({url}){sep}{date}")
    return content + "\n" + "\n".join(lines) + "\n"


def references_section(report: str) -> str:
    """从已 finalize 的报告里抽出 ## 参考 章节（含标题），供流式回显补打。

    流式模式下正文已由 chat_stream echo（用户看到的是原始 [sN] 版本），
    finalize 重映射后的正文编号与参考列表用户看不到——调用方用此函数只补打
    参考章节，避免把整份报告重复打印一遍。
    """
    idx = report.find("## 参考")
    if idx < 0:
        return ""
    return report[idx:].rstrip() + "\n"
