# -*- coding: utf-8 -*-
"""
CLI REPL 入口：双路径调研智能体（ReAct / Plan-Execute）+ 全程流式。

用法：
    python repl.py

进入交互循环后，直接输入问题即可：
    - 简单 / 探索型问题   → 自动走 ReAct 路径（边想边查）。
    - 复杂 / 多维度任务   → 自动走 Plan-Execute 路径（先规划再执行）。

命令：
    /help            显示帮助
    /stream on|off   开关流式输出（默认 on）
    /exit  /quit     退出（也可输入 exit / quit，或按 Ctrl+C）
"""

from datetime import datetime

from agent import config, dag_store
from agent.orchestrator import run_plan_dag
from agent.plan_execute import run_plan_and_execute
from agent.react import run_react
from agent.router import classify_route

BANNER = f"""============================================================
  双路径调研智能体（ReAct / Plan-Execute）· 全程流式
  模型：{config.MODEL}   Plan 引擎：DAG 并行+反思（可用 /engine 切换）
  输入问题即可，系统会自动判断走哪条路径。
  输入 /help 查看命令，/exit 退出。
============================================================"""

HELP = """命令：
  /help            显示本帮助
  /stream on|off   开关流式输出（默认 on）
  /engine dag|linear  切换 Plan 路径引擎：dag=DAG并行+反思（默认），linear=线性旧版
  /resume <run_id> 恢复 runs/<run_id> 的中断任务（跳过已完成节点继续）
  /runs            列出所有已有 run_id
  /exit  /quit     退出（也可输入 exit / quit，或按 Ctrl+C）

路径说明：
  ReAct         简单 / 探索型问题，边想边查（调用联网搜索）。
  Plan-Execute  复杂 / 多维度任务，先出调研 DAG，分层并发执行 + 逐子任务反思 + 分层重规划，最后汇总成报告。
"""

ROUTE_LABEL = {"react": "ReAct", "plan": "Plan-Execute"}
DIVIDER = "-" * 60


def handle_task(task: str, stream: bool, engine: str) -> None:
    """对单个任务做路由并分发执行，单任务异常不中断 REPL。"""
    route, reason = classify_route(task)
    print(f"🧭 路由：{ROUTE_LABEL.get(route, route)}（{reason}）\n")

    if route == "react":
        run_react(task, stream=stream)
    elif engine == "linear":
        run_plan_and_execute(task, stream=stream)
    else:
        run_id = dag_store.make_run_id(task, datetime.now().strftime("%Y%m%d-%H%M%S"))
        print(f"📁 run_id: {run_id}（中断后可用 /resume {run_id} 恢复）")
        run_plan_dag(task, stream=stream, run_id=run_id)


def main() -> None:
    stream = True
    engine = "dag"
    print(BANNER)

    while True:
        try:
            user_input = input("\n你> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见！")
            break

        if not user_input:
            continue

        lowered = user_input.lower()
        if lowered in ("/exit", "/quit", "exit", "quit"):
            print("再见！")
            break
        if lowered == "/help":
            print(HELP)
            continue
        if lowered == "/runs":
            runs = dag_store.list_runs()
            print("已有 run：" + ("、".join(runs) if runs else "（无）"))
            continue
        if lowered.startswith("/stream"):
            parts = lowered.split()
            if len(parts) == 2 and parts[1] in ("on", "off"):
                stream = parts[1] == "on"
                print(f"流式输出已{'开启' if stream else '关闭'}")
            else:
                print("用法：/stream on 或 /stream off")
            continue
        if lowered.startswith("/engine"):
            parts = lowered.split()
            if len(parts) == 2 and parts[1] in ("dag", "linear"):
                engine = parts[1]
                print(f"Plan 引擎已切换为：{'DAG 并行+反思' if engine == 'dag' else '线性旧版'}")
            else:
                print("用法：/engine dag 或 /engine linear")
            continue
        if lowered.startswith("/resume"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                rid = parts[1].strip()
                if dag_store.has_state(rid):
                    print(DIVIDER)
                    try:
                        run_plan_dag("", stream=stream, run_id=rid, resume=True)
                    except Exception as e:  # noqa: BLE001
                        print(f"\n[恢复执行出错] {e}")
                    print(DIVIDER)
                else:
                    print(f"未找到 runs/{rid} 的执行状态（用 /runs 查看可用 run_id）")
            else:
                print("用法：/resume <run_id>")
            continue

        # 正常任务：路由 + 执行，单任务失败不退出会话
        print(DIVIDER)
        try:
            handle_task(config.clean_text(user_input), stream, engine)
        except KeyboardInterrupt:
            print("\n（已中断当前任务，回到输入）")
        except Exception as e:  # noqa: BLE001 - REPL 需要兜底，保证会话不中断
            print(f"\n[任务执行出错] {e}\n（可继续输入下一个问题）")
        print(DIVIDER)


if __name__ == "__main__":
    main()
