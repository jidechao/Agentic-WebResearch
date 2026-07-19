# ADR-0002: 收敛 ReAct 循环为单一深模块 react_loop

- 状态：已接受
- 日期：2026-07-18
- 决策者：与用户 grilling 后共同确认

## 背景

同一个 ReAct 循环（想—做—看）+ 工具执行块在三处近乎逐行复制：
`agent/react.py`、`agent/plan_execute.py`、`agent/plan_dag.py`（另有第四份在独立脚本
`plan_execute_agent.py`）。三份已开始各自漂移——`plan_dag.py` 的版本丢了参数解析
失败的警告打印。改一处要改三处，漏改就是 bug。

## 决策

收敛为单一深模块 `agent/react_loop.py`：

```python
react_loop(user_content, *, system_prompt, tools, execute_tool,
           max_rounds, stream) -> Reply
```

interface 窄（一个函数 + 一个工具回调约定）；implementation 深（循环、tool_calls
判断、assistant 消息重建、JSON 解析兜底、轮数耗尽强制总结、流式/非流式分支，
全部封在内部）。三个调用方只传旋钮。

## 关键取舍

1. **流式/非流式做成模块内部分支**，调用方无感，接口最窄。
2. **工具回调暴露**：`execute_tool(name, arguments: dict) -> str`，支持挂任意工具；
   JSON 解析归模块内部，失败直接兜底给模型（不调用回调），回调只收合法 dict。
3. **web_search 用薄适配器**（`tools.web_search_tool`）做协议翻译，而非改造
   `web_search` 接收 dict——搜索实现保持单一职责，不被工具协议污染；
   adapter 住在 seam 上。将来加新工具各自有适配器。
4. **assistant 消息重建收进模块**（唯一需要 OpenAI 结构的地方），不污染数据模型。

## 后果

- 删除三处重复循环 ~140 行（react.py 从 95 → 33 行）。
- 工具执行错误处理在三路径间不再漂移。
- 测试命中一个 interface，而非三份。
- 已验证：DAG 路径 4 节点并发 + 反思 + 强制总结分支在真实 API 下正常。

## 关联

- 依赖 [[0001-unify-llm-reply]]（react_loop 内部只面对统一 Reply）。
