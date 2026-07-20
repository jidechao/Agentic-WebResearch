# AGENTS.md

本文件面向在本仓库工作的 Agent（Codex / Claude Code 等）。约定与领域词汇见 `CONTEXT.md`，架构决策见 `docs/adr/`。

## Agent skills

### Issue tracker

Issue 以 GitHub issue 形式托管于 `jidechao/Agentic-WebResearch`，经 `gh` CLI 操作。见 `docs/agents/issue-tracker.md`。

### Triage labels

沿用 Matt Pocock 五个标准 triage 标签（`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`）。见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文：根目录 `CONTEXT.md` + `docs/adr/`。见 `docs/agents/domain.md`。
