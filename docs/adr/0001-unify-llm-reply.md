# ADR-0001: 统一 LLM 返回形状（Reply / ToolCall）

- 状态：已接受
- 日期：2026-07-18
- 决策者：与用户 grilling 后共同确认

## 背景

`agent/llm.py` 曾暴露两个返回形状：`chat()` 返回裸 OpenAI SDK `Completion`，
`chat_stream()` 返回自定义 `StreamedMessage`。结果 5 个模块、12 处直接伸手
`.choices[0].message.content` / `.tool_calls` / `.finish_reason`——SDK 的对象
结构泄漏穿过 seam，换供应商要动遍所有调用方。

## 决策

`chat()` 与 `chat_stream()` 统一返回 `Reply(content, tool_calls, finish_reason)`。
SDK 对象结构封在 seam 内部，调用方只接触 `reply.content / .tool_calls / .finish_reason`。

`ToolCall` 为纯数据三字段：`id / name / raw_arguments(str)`。

## 关键取舍：ToolCall 不预解析、不带方法

曾被推翻的过度设计（grilling 中用户质疑"你确信这样合理吗"后修正）：
- ~~`ToolCall` 挂 `to_message_dict()`~~ —— 重建 assistant 消息只有 `react_loop`
  一个调用方需要，把单一调用方的需求泛化进数据模型是浅模块味道。
- ~~`arguments` 预解析成 dict~~ —— 重建消息需要的是原始 JSON 字符串，
  预解析后还得 `json.dumps` 回去，白做功。

**结论**：`ToolCall` 存原始字段；assistant 消息重建逻辑收进 `react_loop`
（唯一需要的调用方）；arguments 的 dict 解析只在执行工具那一步做，用完即弃。

## 后果

- 12 处 `.choices[0].message.*` 全部消除，seam 收紧。
- 换供应商只改 `llm.py` 一个模块。
- 测试可注入假 `Reply`，无需摸 SDK 结构（`test_dag_unit.py` 的 critic mock 已受益）。
