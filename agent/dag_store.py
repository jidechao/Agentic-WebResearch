# -*- coding: utf-8 -*-
"""
DAG 计划与执行状态的落盘 / 恢复。

存储布局（runs/<run_id>/）：
    plan.json    当前 DAG 计划（make_dag_plan 产出后写入，replan 后更新）
    state.json   执行进度（completed: {id: result}, remaining: [节点], layer）
    report.md    最终报告

写盘均为原子写（先写临时文件再 os.replace），防止崩溃留下半个 JSON。
"""

import hashlib
import json
import os

from . import config


def make_run_id(task: str, timestamp: str) -> str:
    """生成 run_id：任务哈希 + 时间戳，避免碰撞且跨进程稳定。

    用 hashlib.md5 而非内置 hash()——后者每次进程启动随机化（PYTHONHASHSEED），
    同一任务跨进程 hash 不同，无法用固定 id 恢复历史 run。
    timestamp 由调用方生成传入（如 REPL 用 datetime.now()），
    引擎内部不直接调时间函数，便于测试与复现。
    """
    digest = hashlib.md5(task.encode("utf-8")).hexdigest()[:8]
    return f"{timestamp}-{digest}"


def run_dir(run_id: str) -> str:
    return os.path.join(config.RUNS_DIR, run_id)


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.replace(tmp, path)
    except OSError as e:
        # Windows 上目标文件被占用（如在编辑器打开）时 os.replace 会 PermissionError。
        # 降级为直接覆盖写；仍失败则打印警告，不中断主流程（数据已在内存）。
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                os.remove(tmp)
            except OSError:
                pass
        except OSError:
            print(f"   ⚠️ 写盘失败（{os.path.basename(path)} 被占用）：{e}")


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------- plan
def save_plan(run_id: str, nodes: list[dict]) -> None:
    _atomic_write(
        os.path.join(run_dir(run_id), "plan.json"),
        json.dumps(nodes, ensure_ascii=False, indent=2),
    )


def load_plan(run_id: str) -> list[dict] | None:
    return _read_json(os.path.join(run_dir(run_id), "plan.json"))


# ---------------------------------------------------------------- state
def save_state(
    run_id: str,
    completed: list[dict],
    remaining: list[dict],
    layer: int,
    task: str = "",
) -> None:
    payload = {
        "task": task,             # 原始任务文本，恢复时用于重规划与汇总
        "completed": completed,   # [{id, subtask, result}, ...]
        "remaining": remaining,   # [{id, subtask, depends_on}, ...]
        "layer": layer,
    }
    _atomic_write(
        os.path.join(run_dir(run_id), "state.json"),
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def load_state(run_id: str) -> dict | None:
    """返回 {completed, remaining, layer} 或 None。"""
    return _read_json(os.path.join(run_dir(run_id), "state.json"))


# ---------------------------------------------------------------- report
def save_report(run_id: str, report: str) -> None:
    _atomic_write(os.path.join(run_dir(run_id), "report.md"), report)


# ---------------------------------------------------------------- evidence
def save_evidence(run_id: str, pool: list[dict]) -> None:
    """落盘证据池（DAG 引擎 /resume 恢复用），与 plan/state 同目录同原子写模式。"""
    _atomic_write(
        os.path.join(run_dir(run_id), "evidence.json"),
        json.dumps(pool, ensure_ascii=False, indent=2),
    )


def load_evidence(run_id: str) -> list[dict] | None:
    """读回证据池；不存在或损坏返回 None（调用方据此走全新 reset）。"""
    return _read_json(os.path.join(run_dir(run_id), "evidence.json"))


def has_state(run_id: str) -> bool:
    return os.path.exists(os.path.join(run_dir(run_id), "state.json"))


def list_runs() -> list[str]:
    """列出所有已有 run_id（按名称排序）。"""
    if not os.path.isdir(config.RUNS_DIR):
        return []
    return sorted(
        d for d in os.listdir(config.RUNS_DIR)
        if os.path.isdir(os.path.join(config.RUNS_DIR, d))
    )
