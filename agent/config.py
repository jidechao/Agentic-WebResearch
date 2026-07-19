# -*- coding: utf-8 -*-
"""
集中配置：
  - 密钥与模型从 .env 读（load_dotenv）。
  - 行为参数（停止条件 + 生成/上下文上限）从 config.toml 读（tomllib），
    缺失回退到内置默认。
config.toml 不存在或某键缺失时，行为与默认完全一致（零回归）。见 ADR-0008。
"""

import os
import sys
import tomllib
from pathlib import Path

from dotenv import load_dotenv

# Windows 控制台默认 GBK，无法打印 emoji / 部分中文，强制 UTF-8 输出/输入
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


def clean_text(s: str) -> str:
    """剔除可能导致 API 编码失败的非法字符（如孤立代理项 surrogates）。"""
    if not s:
        return s
    return s.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")


# ================================================================ 外部配置加载
# 密钥 + 模型：.env
load_dotenv()


def _load_overrides(path: Path | None = None) -> dict:
    """读取 config.toml 覆盖项；文件不存在或损坏则返回 {}（全用内置默认）。

    path 默认指向项目根的 config.toml；测试可传临时路径。
    """
    p = path if path is not None else (Path(__file__).resolve().parent.parent / "config.toml")
    if not p.exists():
        return {}
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
        return data if isinstance(data, dict) else {}
    except (tomllib.TOMLDecodeError, OSError) as e:
        print(f"⚠️ config.toml 解析失败，忽略并用内置默认：{e}")
        return {}


_O = _load_overrides()  # 覆盖项；空 dict = 全用内置默认


def _o_int(section: str, key: str, default: int) -> int:
    v = _O.get(section, {}).get(key)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        print(f"⚠️ config.toml [{section}].{key} = {v!r} 不是合法整数，用默认 {default}")
        return default


def _o_float(section: str, key: str, default: float) -> float:
    v = _O.get(section, {}).get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        print(f"⚠️ config.toml [{section}].{key} = {v!r} 不是合法浮点，用默认 {default}")
        return default


def _o_str(section: str, key: str, default: str) -> str:
    v = _O.get(section, {}).get(key)
    return default if v is None else str(v)


# ================================================================ 密钥（fail-fast）
# security.md：密钥缺失必须在启动即失败（os.environ[...] → KeyError），
# 而非静默默认空串、把问题推迟成 401 / 空搜索。
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
BOCHA_API_KEY = os.environ["BOCHA_API_KEY"]

# ================================================================ 端点 / 模型
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
BOCHA_URL = "https://api.bochaai.com/v1/web-search"
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

# ================================================================ 行为参数（config.toml 可覆盖，默认=历史值）
# [limits] 循环迭代上限（智能体的"停止条件"）
MAX_REACT_TASK_ROUNDS = _o_int("limits", "react_task_rounds", 6)   # ReAct 顶层任务最大轮数
MAX_REACT_ROUNDS      = _o_int("limits", "react_rounds", 3)        # 单个子任务的 ReAct 轮数
MAX_PLAN_STEPS        = _o_int("limits", "plan_steps", 8)          # 线性引擎计划长度硬上限
MAX_DAG_NODES         = _o_int("limits", "dag_nodes", 8)           # DAG 节点数硬上限
MAX_REFLECT_RETRIES   = _o_int("limits", "reflect_retries", 2)     # 子任务反思打回重执次数
MAX_REPORT_RETRIES    = _o_int("limits", "report_retries", 1)      # 报告级 Reviewer 重写次数

# [review] 通过阈值 + 审查输入截断
CRITIC_PASS_SCORE  = _o_int("review", "critic_pass_score", 7)      # Critic / 报告 Reviewer 放行阈值（1-10）
CRITIC_RESULT_LEN  = _o_int("review", "critic_result_chars", 2000) # 喂给 Critic 的 result 截断
REPORT_REVIEW_LEN  = _o_int("review", "report_review_chars", 4000) # 喂给报告 Reviewer 的 report 截断

# [llm] 模型调用
LLM_RETRIES       = _o_int("llm", "retries", 3)                    # 失败重试次数（指数退避）
LLM_TIMEOUT       = _o_float("llm", "timeout", 120.0)              # client 超时秒
LLM_MAX_TOKENS    = _o_int("llm", "max_tokens", 4096)              # 默认生成长度上限
REPORT_MAX_TOKENS = _o_int("llm", "report_max_tokens", 8192)       # 报告生成长度上限

# [search] 联网搜索
MAX_SEARCH_RESULTS = _o_int("search", "max_results", 5)            # 每次返回网页条数
SEARCH_TIMEOUT     = _o_int("search", "timeout", 30)               # 博查 API 超时秒
SEARCH_CONTENT_LEN = _o_int("search", "content_chars", 500)        # 单条结果摘要截断
SEARCH_FRESHNESS   = _o_str("search", "freshness", "oneYear")      # 时效限定
UPSTREAM_BRIEF_LEN = _o_int("search", "upstream_brief_chars", 300)  # 注入下游的上游摘要长度

# ---------------------------------------------------------------- 其它
RUNS_DIR = "runs"           # 计划/状态落盘根目录
