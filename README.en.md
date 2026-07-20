# Dual-Path Research Agent (ReAct / Plan-Execute)

**[简体中文](README.md) | [English](README.en.md)**

> A **web research agent** that "thinks before it acts." It **dynamically routes** each question to one of two execution paths — lightweight think-and-search (ReAct), or plan-then-execute (Plan-Execute) — and by default runs a production-grade pipeline of **DAG layered concurrency + double-loop reflection + resumable plan persistence**. Fully streamed, CLI REPL, built on DeepSeek + Bocha web search. The final report ships with **traceable citations** — inline `[N]` markers + a trailing reference list, with links sourced directly from the Bocha API by the search-tool layer (zero hallucination).

This document is for developers who want to **understand the internals, extend, or contribute**. Beyond the quick start, the second half dissects every mechanism: routing, the DAG pipeline, double-loop reflection, resumable persistence, and the LLM call layer.

> Origin: a reimplementation and deepening of the ideas in two articles —
> - *"Why does your AI agent keep going off-track? Maybe it 'doesn't know how to plan'"* — motivates Plan-and-Execute with dynamic replanning.
> - *"More agents isn't stronger: I debunked AI agent collaboration with 100 lines of Python"* — motivates "communication protocol determines the ceiling": raw-requirement passthrough, structured messages, double-loop review.
>
> This project architecturalizes those ideas (see `docs/adr/`).

---

## Table of Contents

- [What Problem It Solves](#what-problem-it-solves)
- [30-Second Overview](#30-second-overview)
- [Architecture](#architecture)
- [End-to-End Workflow (Core)](#end-to-end-workflow-core)
- [Core Mechanisms, Dissected](#core-mechanisms-dissected)
- [Design Principles (Why It's Written This Way)](#design-principles-why-its-written-this-way)
- [Quick Start](#quick-start)
- [REPL Commands](#repl-commands)
- [Configuration](#configuration-agentconfigpy)
- [Project Structure](#project-structure)
- [Testing](#testing)
- [How to Extend](#how-to-extend)
- [Architecture Decision Records (ADR)](#architecture-decision-records-adr)
- [Limitations & Roadmap](#limitations--roadmap)
- [License](#license)

---

## What Problem It Solves

A **pure ReAct agent** "takes one step at a time and looks" with no global blueprint. It tends to drill ever deeper into one local topic and drift off-task — ask it to "survey this year's battery-tech advances" and it may fixate on "solid-state batteries" and search forever, forgetting the task wanted a panoramic sweep.

This project solves that drift with **Plan-and-Execute + dynamic replanning**, and auto-selects the most fitting execution mode per question type:

- **Simple / exploratory questions** ("today's weather", "look up an error") → **ReAct**, one-shot, cheap.
- **Complex / multi-dimensional tasks** ("write a research report", "compare across dimensions") → **Plan-Execute**, draw the blueprint first, then build to it.

Every output stage gets a **reflection guardrail**, and the **user's original request is never lost** in transit — closing off the silent "every step right, overall wrong" drift before delivery.

---

## 30-Second Overview

```
You type a question
      │
      ▼
  Route classifier (1 LLM call) ─── simple or complex?
      │
      ├── simple ──▶ ReAct: think-and-search (≤6 rounds of web_search) ──▶ direct answer
      │
      └── complex ──▶ Plan-Execute (DAG engine, default):
                       ① Plan: decompose the task into a dependency DAG (which steps, who depends on whom)
                       ② Execute: layer by dependency; concurrent within a layer; each step
                                  vetted by a Critic — redo if it doesn't pass
                       ③ Replan: after each layer, adjust remaining plan using new findings
                       ④ Summarize: write a report, then a report-level Reviewer checks the
                                    whole thing (gaps trigger targeted research + rewrite)
                       ⑤ Cite: before persisting, pin search results as [N] markers + a reference list
                       Everything persists to disk; interrupt with /resume to continue
```

---

## Architecture

### Module Dependency Graph

Dependencies are **one-way and acyclic**: upper layers orchestrate, lower layers provide capability; no module depends back on a caller.

```
                         repl.py  (CLI REPL entry: routing dispatch / commands / exception isolation)
                            │
                       router.py  (question-type understanding → react / plan)
                      ╱          ╲
            ┌──── ReAct ────┐   ┌─────────── Plan-Execute ───────────┐
            │               │   │                                     │
         react.py    plan_execute.py             orchestrator.py
         (top-level)  (linear engine /engine linear)    (DAG engine · default)
            │               │                   ╱          ╲
            └────────┬──────┘            planner.py   execute_reflect.py
                     │                  (planning/replan/   (execution + double-loop
                  react_loop.py          gap-fill +           reflection: subtask Critic +
                (ReAct loop deep module)  pure graph logic)    report-level Reviewer)
                     │                                          │
   ┌─────────────────┴──────────────────────────────────────────┐
   llm.py        tools.py        evidence.py     dag_store.py    parsing.py    prompts.py
  (unified       (web_search     (atomic           (robust JSON    (shared
   Reply +       + tool adapter)  persistence       parsing /       prompts)
   lazy client)                   + resume)         semantic diff)
                                                                config.py
                                                            (config + constants)
```

### Module Responsibilities

| Module | Responsibility | Key functions |
|------|------|----------|
| `repl.py` | CLI entry, command parsing, routing dispatch, per-task exception isolation | `main`, `handle_task` |
| `agent/router.py` | Question type → route (1 LLM call) | `classify_route` |
| `agent/react.py` | ReAct top-level path (thin shell) | `run_react` |
| `agent/plan_execute.py` | Linear Plan-Execute engine | `run_plan_and_execute` |
| `agent/orchestrator.py` | DAG engine main flow + persistence + report reflection | `run_plan_dag`, `_run_dag_async`, `_finalize` |
| `agent/planner.py` | DAG planning/replan/gap-fill + pure logic (topo/cycle-break) | `make_dag_plan`, `replan_dag`, `plan_missing`, `topo_layers`, `repair_and_acyclic` |
| `agent/execute_reflect.py` | Subtask execution + double-loop reflection | `execute_subtask`, `critic_review`, `report_review`, `run_with_reflection` |
| `agent/react_loop.py` | ReAct loop deep module (reused by 3 callers) | `react_loop` |
| `agent/llm.py` | Unified Reply/ToolCall + chat/chat_stream (lazy client) | `chat`, `chat_stream`, `Reply`, `ToolCall` |
| `agent/tools.py` | web_search + tool-protocol adapter | `web_search`, `web_search_tool`, `SEARCH_TOOL_SCHEMA` |
| `agent/evidence.py` | Evidence pool (citation backend) | `add_evidence`, `finalize_report`, `reset_evidence`, `get_pool` |
| `agent/dag_store.py` | Atomic persistence of plan/state/report + resume | `save_*`, `load_*`, `make_run_id` |
| `agent/parsing.py` | Robust JSON parsing, plan normalization, semantic diff | `parse_json_object`, `parse_json_array`, `plans_differ` |
| `agent/prompts.py` | Shared prompts (subtask system prompt / summary) | `SUBTASK_SYSTEM`, `summary_prompt` |
| `agent/config.py` | Config + behavior constants + console UTF-8 fix | `MODEL`, `MAX_*`, `clean_text` |

---

## End-to-End Workflow (Core)

This is the section developers most need to internalize. We trace the full path from "user types a line" to "final report," explaining at each stage what happens, which functions are called, and what the data looks like.

### Stage 0: REPL entry (`repl.py`)

`main()` holds two pieces of session state: `stream` (streaming on/off, default on) and `engine` (`dag` default / `linear`).

For each input line:
- Slash commands (`/help` `/stream` `/engine` `/resume` `/runs` `/exit`) are handled in place.
- Plain text → `config.clean_text()` strips isolated surrogates (prevents API encoding failures) → `handle_task()`.
- **Exception isolation**: any exception in a single task (including `KeyboardInterrupt`) is caught by `try/except` — **only the current task ends; the session never dies**.

`handle_task(task, stream, engine)`:

```
route, reason = classify_route(task)          # 1 classification LLM call
if route == "react":       run_react(task, stream)
elif engine == "linear":   run_plan_and_execute(task, stream)
else:                      run_plan_dag(task, stream, run_id)   # DAG default
```

### Stage 1: Route classification (`router.py`)

`classify_route(task) -> (route, reason)` uses **one non-streaming LLM call with thinking disabled** (cheaper, faster), judging by "are the step count fixed / does it depend on intermediate results / is it multi-dimensional":

- → `react`: single-fact / instant Q&A / exploratory.
- → `plan`: well-scoped, multiple parallel/dependent sub-goals.

Outputs strict JSON `{"route","reason"}`. **Fallback**: on parse failure or an invalid value it defaults to `plan` (complex tasks tolerate the heavier path better — better to over-plan than under-plan).

### Stage 2a: ReAct path (`react.py` → `react_loop.py`)

`run_react` is a thin shell: it sets the top-level system prompt and `MAX_REACT_TASK_ROUNDS=6`, then calls the deep module `react_loop`.

**`react_loop` is the project's loop deep module** (reused by three callers: ReAct top-level / linear-engine subtasks / DAG-engine subtasks). Internally it runs the classic "think–act–observe":

```
messages = [system, user]
for round in range(max_rounds):
    reply = chat or chat_stream (per stream flag)
    if reply has no tool_calls:          # model stops calling tools = final answer
        return reply
    # otherwise: execute tools, feed results back into context
    execute all tool_calls:
        - on JSON arg parse failure → do NOT call with empty args; instead feed
          "invalid arguments" back to the model so it self-corrects next round
        - on success → execute_tool(name, arguments); append result as a tool message
# rounds exhausted: drop tools and call once more, forcing a summary from collected info
```

Key point: **tool-argument parsing lives in `react_loop`** — on failure it **never calls the tool with an empty query**, but instead prompts the model to correct — wasting no search.

### Stage 2b: Plan-Execute · DAG engine (default, the meat)

Entry `run_plan_dag(task, stream, run_id, resume=False)`, four steps:

#### ① Planning: `planner.make_dag_plan(task)`

Not an ordered list, but a **dependency DAG with `depends_on`**:

```
LLM emits JSON array [{id, subtask, depends_on}, ...]
    ↓ extract_items        (handles standard array / NDJSON / scattered objects)
    ↓ normalize_nodes      (flattens mixed str/dict; assigns fresh ids if missing/duplicate)
    ↓ repair_and_acyclic   (strip dangling deps + DFS tri-color cycle breaking)
    ↓ truncate to MAX_DAG_NODES
```

Example: `survey sodium-ion vs solid-state batteries and compare` →
```
[t1] survey sodium-ion status      depends_on: []
[t2] survey solid-state status     depends_on: []
[t3] synthesize comparison         depends_on: [t1, t2]   ← must wait for t1/t2
```

`resume` branch: restores `completed`/`remaining` from `state.json`; empty/corrupt → replan; if the original task is missing → explicit error (no task means no replan or summary possible).

#### ② Layered concurrent execution: `orchestrator._run_dag_async`

```
executed_total = len(completed)              # budget baseline (completed counts too)
while remaining and executed_total < MAX_DAG_NODES:
    layers = topo_layers(remaining, done=set of completed ids)   # Kahn BFS layering
    for layer in layers:
        ▶ asyncio.gather(                                        # concurrent within layer
              asyncio.to_thread(run_with_reflection, n, upstream_briefs, task)
          )
        ▶ each node's result appended to completed; executed_total++
        ▶ save_state  (persist; interrupt-safe)
        ▶ if remaining and budget remains:
              new_remaining = replan_dag(task, completed, remaining, budget)
              if plans_differ(new_remaining, remaining):   # semantic diff; ignore mere rewording
                  remaining = new_remaining; save_plan; break (re-layer)
```

**Why `asyncio.to_thread` instead of httpx/aiohttp?** The project depends only on the synchronous OpenAI SDK; `to_thread` wraps sync calls into concurrently-runnable units — **no new dependency**, controlled blast radius. Layers stay sequential (lower layers depend on upper-layer results).

#### ③ Per-node execution + subtask reflection: `execute_reflect.run_with_reflection`

Each node's execution is a closed loop of **execute → evaluate → redo-if-substandard**:

```
for attempt in range(MAX_REFLECT_RETRIES + 1):       # default: up to 2 redos
    result = execute_subtask(subtask, upstream_briefs, feedback, original_task=task)
    verdict = critic_review(subtask, result, original_task=task)
    if verdict["pass"]: return result
    feedback = f"issues: {issues}\nadvice: {suggestion}"   # redo with the critique
# retries exhausted → accept current result (print a warning)
```

- **`execute_subtask`** assembles user content = `## User's original request` + subtask + upstream briefs + feedback, then calls `react_loop` (non-streaming, to avoid interleaving across concurrent tasks).
- **`critic_review`** scores on coverage / accuracy / evidence / relevance, returning `{pass, score, issues, suggestion}`; **score is a hard gate** (≥ `CRITIC_PASS_SCORE` to pass, guarding against contradictory "pass:true but low score" output); parse failure defaults to pass.

> **Key design (ADR-0005)**: both `execute_subtask` and `critic_review` receive `original_task` — the user's raw text reaches every execution/review call. The Critic sees both the subtask and the original request, acting as the user's advocate: if output satisfies the subtask but drifts from the original (drops a constraint, answers the wrong question), it fails. This is the key to closing off silent "telephone-game" constraint loss.

#### ④ Report-level reflection loop: `orchestrator._finalize` (ADR-0006, the lower half of double-loop reflection)

After all nodes finish, the results are assembled into a final report that passes through one more **holistic review**:

```
for attempt in range(MAX_REPORT_RETRIES + 1):       # default: up to 1 rewrite
    report = _summarize(task, completed, stream, feedback)
    verdict = report_review(task, report, completed)
    if verdict["pass"]: break
    if retry budget exhausted: accept current report; break
    missing = verdict["missing_constraints"]
    if missing and node budget remains:
        new_nodes = planner.plan_missing(task, missing, completed)   # gap-fill nodes
        completed = asyncio.run(_run_dag_async(task, new_nodes, completed, run_id))  # reuse executor
    feedback = f"issues: {issues}\nadvice: {suggestion}"   # rewrite with feedback
save_report(run_id, report)
```

`report_review` distinguishes two failure types:
- **Synthesis defect** (material exists but the report didn't assemble it well — e.g., the "comparison" section didn't actually compare) → written to `issues/suggestion`, **rewrite with feedback**.
- **Coverage gap** (an original-request constraint appears neither in the report nor in any subtask's material — e.g., "list the mass-production timeline separately" was never planned as a subtask) → listed in `missing_constraints`, **targeted research via `plan_missing`, then rewrite**.

> The subtask-level Critic catches "single-step errors"; the report-level Reviewer catches "system-level errors" — and the latter cannot, structurally, be caught by the former (every subtask may pass individually while the assembly still drops a constraint). Together they form the **raw-request alignment chain: executor → critic → report**.

### Stage 2c: Plan-Execute · linear engine (`/engine linear`)

`plan_execute.run_plan_and_execute` is the earlier linear implementation (kept for comparison and lightweight cases):

- `make_plan` produces an ordered `list[str]` (3–5 steps, no dependency graph).
- Sequential execution of `execute_subtask(subtask, stream, original_task=task)`; after each step (except the last), `replan_if_needed` adjusts the remaining list from completed results.
- The tail runs the same report-level reflection loop (symmetric with the DAG engine).
- **Note**: the linear engine **has no per-subtask Critic** — its only quality gate is the report-level Reviewer.

### Stage 3: Output & persistence

- ReAct path: prints `reply.content` directly to the user; before returning, `evidence.finalize_report` remaps the runtime `[sN]` anchors to contiguous `[1]..[n]` display numbers and appends the reference list.
- Plan-Execute: the final report is streamed **and** persisted to `runs/<run_id>/report.md`; before persist it also runs `finalize_report`, and the evidence pool is saved to `evidence.json` (DAG engine only, for /resume backfill).

---

## Core Mechanisms, Dissected

### 1. DAG planning & validation (pure logic, offline-testable)

`planner.py` separates **LLM-driven planning** from **LLM-free graph logic** at the function level — the latter is directly offline-testable with no mocking:

| Function | Purpose |
|------|------|
| `extract_items(text)` | Pull nodes from model output; tolerates standard JSON array / NDJSON / scattered objects |
| `normalize_nodes(raw)` | Flatten mixed str/dict input to `[{id, subtask, depends_on}]`; assign fresh ids when missing/duplicate |
| `repair_and_acyclic(nodes)` | Strip dangling and self-dependencies; **DFS tri-color cycle breaking** (delete back-edges that form cycles, degrading to acyclic) |
| `topo_layers(nodes, done)` | **Kahn BFS layering**: layer 0 has no deps, layer k's deps are all in shallower layers; the `done` set lets the resume case treat "already-completed deps" as satisfied |

When `replan_dag` adjusts the remaining plan, its `extra_valid_ids` parameter lets new nodes depend on **already-completed nodes** (otherwise `repair_and_acyclic` would mistake them for dangling and delete them).

### 2. Layered concurrency

`asyncio.gather` + `asyncio.to_thread` gives concurrency within a layer and serialization between layers. **Concurrent layers are uniformly non-streaming** (to avoid tangled multi-task streaming output); single-node layers and the final report stay streamed.

### 3. Upstream injection (relay communication)

When a subtask executes, the **result briefs** of its `depends_on` upstream nodes (truncated to `UPSTREAM_BRIEF_LEN=300` chars) are spliced into its prompt — breaking "information silos between subtasks." For example, when `t3 compare` runs it can see the conclusions of `t1` and `t2`, avoiding duplicate work.

This is the **relay** part of the article's "blackboard + relay hybrid topology": real upstream results (not the planner's paraphrase) flow downstream.

### 4. Double-loop reflection

- **Subtask-level Critic** (`critic_review`): evaluates each step's output, redoes with critique if substandard (≤ `MAX_REFLECT_RETRIES=2`).
- **Report-level Reviewer** (`report_review`): holistic review of the final report; synthesis defects get a rewrite-with-feedback, coverage gaps get targeted research (≤ `MAX_REPORT_RETRIES=1`).

Both layers **align to the original request** (ADR-0005), and the Critic/Reviewer are independent `chat()` calls that **see only the result, not the execution trace** — this context isolation yields sharper criticism (the reviewer carries no baggage from "what it just wrote").

### 5. Raw-requirement passthrough (ADR-0005)

The user's raw text `task` is **passed verbatim** into the executor's and critic's prompts (not used once at the planner and then dropped). In the article's words: the planner's instruction is the "navigation," the user's raw request is the "map" — when navigation is wrong, the executor can still self-correct from the map. The cost is a few hundred extra tokens — negligible against the cost of rework.

### 6. Plan persistence & resumable interruption (`dag_store.py`)

Every run atomically writes to `runs/<run_id>/` in real time:

| File | Contents |
|------|------|
| `plan.json` | The current DAG plan (updated on replan) |
| `state.json` | `{task, completed:[{id,subtask,result}], remaining:[nodes], layer}` |
| `report.md` | The final report (post-citation) |
| `evidence.json` | Evidence pool (DAG engine only; /resume backfill) |

- **Atomic writes**: write `.tmp` then `os.replace`; on Windows if the target file is locked, degrade to a direct write with a warning — never crash the main flow.
- **`run_id` is stable across processes**: `md5(task)[:8] + timestamp`, **not built-in `hash()`** (which is randomized per process via `PYTHONHASHSEED`, making the same task hash differently across processes and unresumable).
- **`/resume <run_id>`**: loads completed state, `topo_layers(done=completed-ids)` skips done nodes and continues — **no wasted tokens**.

### 7. Unified Reply shape + lazy client + current-date injection (`llm.py`, ADR-0001/0003/0007)

- **Unified shape**: both `chat` and `chat_stream` return `Reply(content, tool_calls, finish_reason)`; `ToolCall(id, name, raw_arguments)`. The OpenAI SDK's `.choices[0].message.*` structure is sealed inside `llm.py` — **callers never touch SDK objects**. This lets tests fake `chat` (the consumer's namespace) rather than injecting a fake SDK client, which is more robust.
- **Lazy construction**: `_get_client()` creates and caches the OpenAI client on first call — no network side effects at import time (after `import agent`, the client is still `None`).
- **Streaming tool_call accumulation**: `_ToolCallBuf` aggregates id/name/arguments fragments by `index` (using `+=` idempotently, tolerating some compatibility layers that re-send or shard); on retry, already-printed content is not re-printed.
- **Thinking mode**: `chat`/`chat_stream` support a `thinking` toggle (DeepSeek reasoning); planning/classification/extraction calls disable it (`extra_body: thinking.disabled` — cheaper and faster), while the ReAct loop and report generation enable it.
- **Retries**: exponential backoff (`2**attempt` seconds), default 3; an explicit `timeout=120s` prevents hanging on a half-open streaming connection.
- **Current-date injection (centralized)**: before building the request, `chat`/`chat_stream` run `_inject_date` to prepend "Current date: YYYY年M月D日 (weekday)" to the system message (merged if one exists, inserted otherwise). The model now knows "today" and no longer hallucinates dates for "today / this year / latest" (it once answered "today" as 2025 when it was actually 2026); covers routing/planning/execution/reflection/report uniformly (ADR-0007).

### 8. Tool layer (`tools.py`)

- **`web_search(query)`**: Bocha Web Search API (DeepSeek's official web-search provider, direct in mainland China). `freshness=oneYear`, `summary=True` (long abstract), `count=MAX_SEARCH_RESULTS`; defensively handles `data:null` / missing layers; truncates each result to 500 chars to avoid context blowup; returns formatted `[n] title / time / source / abstract`.
- **`web_search_tool(name, arguments)`**: **the tool-protocol adapter** — translates the LLM's `(name, arguments dict)` into `web_search(query)`. `web_search` keeps a single responsibility (it only knows how to search) and is not polluted by the generic tool protocol. **When you add new tools, each gets its own adapter**, dispatched in the `react_loop` `execute_tool` callback.
- **`SEARCH_TOOL_SCHEMA`**: the function-calling tools schema.

### 9. Robust JSON parsing (`parsing.py`)

Models often wrap "JSON only" output in ```json fences or add prose. `_extract_json` proceeds: strip fences → `json.loads` → regex-extract the container and `loads` again.

`plans_differ` is a **semantic comparison** (strips punctuation/space/case before comparing) — LLM rewording always changes surface form, so a literal `!=` would falsely flag "plan changed" every round and burn a replan for nothing.

### 10. Robustness guardrails (across the stack)

| Guardrail | Location |
|------|------|
| Hard cap on every loop | ReAct 6 rounds / subtask ReAct 3 / reflection redo 2 / report rewrite 1 / DAG 8 nodes |
| LLM retries + explicit timeout | `llm.chat`/`chat_stream` (120s, exponential backoff ×3) |
| DAG validation | cycle detection & breaking, dangling-dep stripping, id repair, completed-deps treated as satisfied on resume |
| Tool-arg parse failure fallback | no empty-query search; prompt the model to correct |
| Budget guard | `_run_dag_async` uses `executed_total` (including replan/gap-fill additions) to prevent unbounded growth |
| Missing keys fail at startup | `os.environ[...]` → `KeyError` (fail-fast, not deferred to a 401) |
| Atomic persistence | `.tmp` + `os.replace`; no half-JSON on crash |
| REPL exception isolation | a single task's exception never ends the session |

### 11. Citation (`evidence.py`, ADR-0009)

The final report gains **structured citations**: inline `[N]` markers in the body + a trailing reference list `[k] [title](URL) · date`. The core decision — **the source of truth for citations is anchored at the search-tool layer, not self-reported by the LLM**:

- **ID assigned at the tool layer**: when `web_search` parses the Bocha API response it calls `add_evidence(title, url, date)` to assign a globally-unique runtime ID `[sN]` and write it to the evidence pool; the model can only copy markers it has seen in ReAct messages and **cannot inject fabricated URLs** (hallucinated links are eliminated at the source).
- **URL-normalization dedup**: before assigning, the URL is normalized (strip fragment, strip utm_ tracking params, lowercase host); the same URL counts once (no duplicate reference entries).
- **Runtime ID vs display ID separation**: the `[sN]` the model sees throughout is globally unique but non-contiguous; before the report is persisted, `finalize_report` scans once and remaps them to contiguous `[1]..[n]` in order of first appearance, then appends the reference section.
- **Phantom-citation deletion**: if the model annotates an `[sN]` absent from the pool (a hallucination), the post-processor deletes it from the body and never lists it — a bare claim is more honest than a fake anchor.
- **Concurrency safety**: `add_evidence`'s "lookup dedup table → assign ID → write" is a check-then-set sequence wrapped in a module-level `threading.Lock` (the DAG engine runs same-layer subtasks via `asyncio.to_thread` calling `web_search` concurrently; the GIL only guarantees single-bytecode atomicity, the lock is what makes dedup hold).
- **Timing**: `finalize_report` runs once **after the reflection loop ends entirely, before persist** — each review round sees the raw `[sN]` so the "evidence sufficiency" dimension can directly judge whether the model annotated citations (poor annotation triggers a rewrite); remapping every round would let the model continue from already-remapped text and corrupt the numbering.
- **No-marker fallback**: if the model never annotates `[sN]`, the report has no reference list (honest degradation, no forced fabrication).
- **Persistence**: the evidence pool is saved alongside the report to `runs/<run_id>/evidence.json`; on `/resume` the pool is backfilled and the counter starts at `len(loaded)+1`, so new IDs never collide with history.

> The evidence pool is module-level global state read/written directly by `web_search`; the three task entry points (react/plan_execute/orchestrator) call `reset_evidence()` when a task begins — callers are unaware, and the ReAct-loop deep module's signature is unchanged.

---

## Design Principles (Why It's Written This Way)

These are the principles hardened over the project's evolution. Please understand them before contributing (details in `docs/adr/`):

1. **No framework**: bare Python + the OpenAI SDK. The article's words — "a framework just wraps those 100 lines more elegantly" — write it once and every decorator's innards stay visible.
2. **Seal the seam with a unified shape**: the OpenAI SDK structure is sealed inside `llm.py`; callers only touch `Reply` (ADR-0001).
3. **Deep modules**: one `react_loop` absorbs three callers; callers pass only knobs (rounds/stream/system-prompt/tools) (ADR-0002).
4. **Separate pure logic from I/O**: `planner`'s topo/cycle-break/normalize don't touch the LLM and are directly offline-testable.
5. **Split modules by business lifecycle**: `planner` (planning) / `execute_reflect` (execution+reflection) / `orchestrator` (orchestration) (ADR-0004).
6. **Raw-requirement passthrough**: the raw request lives in every execution/review call (ADR-0005).
7. **Double-loop reflection**: subtask Critic + report Reviewer, aligned to the raw request, form a complete defense (ADR-0006).
8. **Strict YAGNI**: cut every speculative design (injectable-client factory, `Node` dataclass, a literal `Blackboard` class, over-abstraction) — see ADR-0003's "not adopted" list.
9. **Fail-fast over silent degradation**: missing keys error immediately, not deferred to a 401 / empty search.
10. **One-way acyclic dependencies**: upper layers orchestrate, lower layers provide, never the reverse.
11. **Anchor citations at the tool layer**: evidence IDs are assigned when search results are parsed, and URLs come from the API rather than LLM self-report; the report post-processor only does mechanical remapping and cannot inject fabricated links (ADR-0009).

---

## Quick Start

### Requirements

- Python 3.11+
- DeepSeek API key ([apply](https://platform.deepseek.com/))
- Bocha search API key ([apply](https://open.bochaai.com/))

### Install

```bash
cd Agentic-WebResearch
python -m venv .venv

# Activate (Windows PowerShell)
.\.venv\Scripts\Activate.ps1
# or (Windows CMD):   .\.venv\Scripts\activate.bat
# or (Linux/macOS):   source .venv/bin/activate

pip install -r requirements.txt
```

`requirements.txt`:

```
openai>=1.0
requests>=2.28
python-dotenv>=1.0
```

### Configure

Create `.env` in the project root:

```env
DEEPSEEK_API_KEY=sk-your-deepseek-key
BOCHA_API_KEY=sk-your-bocha-key
DEEPSEEK_MODEL=deepseek-v4-flash
```

> Missing keys raise at startup (fail-fast). `DEEPSEEK_MODEL` can be any model your account supports (e.g. `deepseek-chat`).

**Behavioral parameters** (optional, defaults work out of the box): to tune stop conditions / limits without editing source, copy the template:

```bash
cp config.example.toml config.toml   # edit config.toml; takes effect on next run
```

`.env` holds keys + model; `config.toml` holds behavioral params (separated). Missing keys fall back to built-in defaults. See [Configuration](#configuration-configexampletoml).

### Run

```bash
python repl.py
```

Type a question directly:

```
============================================================
  Dual-Path Research Agent (ReAct / Plan-Execute) · fully streamed
  Model: deepseek-v4-flash   Plan engine: DAG parallel + reflection
============================================================

你> 今天北京天气怎么样
🧭 Route: ReAct (single-fact query, instant Q&A)
🔍 Search: 北京今天天气
Sunny today, high 7°C ...

你> 调研2025年钠离子电池和固态电池进展并对比
🧭 Route: Plan-Execute (multi-sub-goal complex task)
📁 run_id: 20260719-164507-237c15be
DAG plan:
   [t1] survey sodium-ion battery progress
   [t2] survey solid-state battery progress
   [t3] synthesize comparison  ⇐ deps ['t1', 't2']
===== Layer 1 · executing 2 nodes concurrently =====
   ...
```

### Try these

- **ReAct will take these**: `今天上海天气怎么样` / `宁德时代最新股价` / `什么是固态电池`
- **Plan-Execute will take these**: `调研2025年钠离子电池和固态电池进展并对比` / `写一份关于今年新能源汽车市场的两千字报告`

---

## REPL Commands

| Command | Effect |
|------|------|
| `/help` | Show help |
| `/stream on\|off` | Toggle streaming output (default on) |
| `/engine dag\|linear` | Switch Plan-path engine: `dag`=DAG parallel+reflection (default), `linear`=legacy linear |
| `/resume <run_id>` | Resume an interrupted task (skips completed nodes) |
| `/runs` | List all historical runs |
| `/exit` `/quit` `Ctrl+C` | Exit |

---

## Configuration (`config.example.toml`)

Every stop condition and limit is tunable in `config.toml` (no source edits). The full, commented
list lives in [`config.example.toml`](config.example.toml); the groups:

| Group | Meaning | Representative keys |
|---|---|---|
| `[limits]` | Loop iteration caps (the agent's stop conditions) | `dag_nodes` `react_task_rounds` `plan_steps` `reflect_retries` `report_retries` |
| `[review]` | Pass threshold + review truncations | `critic_pass_score` `critic_result_chars` `report_review_chars` |
| `[llm]` | Model calls | `retries` `timeout` `max_tokens` `report_max_tokens` |
| `[search]` | Web search | `max_results` `timeout` `content_chars` `freshness` `upstream_brief_chars` |

Copy to use: `cp config.example.toml config.toml`. Missing keys fall back to built-in defaults
(`agent/config.py`); invalid values warn at startup and fall back — never crash. Keys + model stay
in `.env`. See [ADR-0008](docs/adr/0008-external-config-toml.md).

---

## Project Structure

```
Agentic-WebResearch/
├── repl.py                      # CLI REPL entry
├── requirements.txt
├── .env                         # Keys & model config (not committed)
├── config.example.toml          # Behavior-param template (copy to config.toml to override; not committed)
├── CONTEXT.md                   # Domain glossary
├── README.md  /  README.en.md   # Chinese / English docs
├── LICENSE
│
├── agent/                       # Core package
│   ├── config.py                #   Config + behavior constants
│   ├── llm.py                   #   Unified Reply/ToolCall + chat/chat_stream (lazy client)
│   ├── parsing.py               #   Robust JSON parsing (array/object, dedup, semantic diff)
│   ├── prompts.py               #   Shared prompts (subtask system / summary)
│   ├── tools.py                 #   web_search + tool adapter (assigns evidence IDs when parsing Bocha results)
│   ├── evidence.py               #   Evidence pool (citation backend: dedup / ID assignment / report post-processing)
│   ├── router.py                #   Question type → route
│   ├── react_loop.py            #   ReAct loop deep module (reused by 3 paths)
│   ├── react.py                 #   ReAct top-level path
│   ├── plan_execute.py          #   Linear Plan-Execute engine (/engine linear)
│   ├── planner.py               #   DAG planning/replan/gap-fill + topo/cycle-break pure logic
│   ├── execute_reflect.py       #   Subtask execution + double-loop reflection (Critic + report Reviewer)
│   ├── orchestrator.py          #   DAG engine orchestration + persistence/resume + report reflection loop
│   └── dag_store.py             #   Atomic persistence of plan/state/report/evidence
│
├── test_dag_unit.py             # Offline tests (DAG logic / cycle detection / topo / reflection parsing / round-trip)
├── test_evidence_unit.py         # Offline tests (evidence pool: URL dedup / ID increment / remapping / phantom deletion)
├── test_fixes_unit.py           # Offline tests (LLM fixes / passthrough / report_review / plan_missing)
├── test_planner_smoke.py        # Real-API smoke (planner connectivity + DAG planning, token-light)
│
├── docs/adr/                    # Architecture Decision Records
│   ├── 0001-unify-llm-reply.md
│   ├── 0002-consolidate-react-loop.md
│   ├── 0003-deferred-candidates.md
│   ├── 0004-split-plan-dag.md
│   ├── 0005-raw-requirement-passthrough.md
│   ├── 0006-report-level-reviewer.md
│   ├── 0007-current-date-injection.md
│   └── 0008-external-config-toml.md
│
└── runs/                        # Runtime artifacts (auto-generated)
    └── <run_id>/
        ├── plan.json
        ├── state.json
        ├── evidence.json            # Evidence pool (DAG engine only; /resume backfill)
    └── report.md                 # Final report (with ## References section)
```

---

## Testing

```bash
# Offline (no real API, seconds) — DAG logic / cycle detection / topo layering / reflection parsing / round-trip
python test_dag_unit.py

# Offline — LLM fixes + raw-requirement passthrough + report Reviewer + gap-fill planning
python test_fixes_unit.py

# Real-API smoke (token-light, one planner call) — DeepSeek connectivity + DAG planning
python test_planner_smoke.py

# Offline — evidence pool: URL-normalization dedup / global ID increment + reset / remapping continuity / phantom deletion
python test_evidence_unit.py
```

For end-to-end verification, run real questions via `python repl.py`.

**Test convention**: no pytest dependency; each `test_*.py` is a standalone runnable script that monkeypatches `chat`/`react_loop` to fake responses and assert key behaviors. Pure logic (`topo_layers`/`repair_and_acyclic` etc.) needs no mocking and is tested directly offline.

---

## How to Extend

### Add a new tool (e.g. `read_url`)

1. Implement the tool function in `tools.py` (e.g. `read_url(url) -> str`) + its schema + an adapter (`read_url_tool(name, arguments)`).
2. Add the schema to the tools list passed to `react_loop` (e.g. `SEARCH_TOOL_SCHEMA + [READ_URL_SCHEMA]`) and dispatch by `name` in the `execute_tool` callback.
3. Tell the model when to use the new tool in the subtask system prompt (`prompts.SUBTASK_SYSTEM`).

The tool protocol is `(name: str, arguments: dict) -> str` — any callable matching this signature can be plugged in.

### Change model / provider

`config.py`'s `DEEPSEEK_MODEL` is overridable via `.env`. To use a non-DeepSeek provider, change `DEEPSEEK_BASE_URL` and the key in `.env` (it uses the OpenAI-compatible interface). **Only when a genuine multi-client / multi-provider need arises** should you consider an injectable-client factory in `llm.py` (explicitly deferred in ADR-0003).

### Add a new execution path

Add a route value to `router.py`'s `VALID_ROUTES`, extend the `classify_route` prompt, and dispatch to the new engine in `repl.handle_task`. Routing is data-driven — adding a path doesn't disturb the existing two.

### Tune reflection intensity

Tweak `config.py`: `MAX_REFLECT_RETRIES` (subtask), `MAX_REPORT_RETRIES` (report), `CRITIC_PASS_SCORE` (pass threshold). Raising `CRITIC_PASS_SCORE` = a stricter quality gate.

---

## Architecture Decision Records (ADR)

Every significant decision (including **rejected alternatives and why**) lives in `docs/adr/`. Read them before contributing:

| ADR | Topic |
|-----|------|
| [0001](docs/adr/0001-unify-llm-reply.md) | Unify the Reply shape; seal the OpenAI SDK behind a seam |
| [0002](docs/adr/0002-consolidate-react-loop.md) | Collapse three ReAct loops into the `react_loop` deep module |
| [0003](docs/adr/0003-deferred-candidates.md) | Architecture-review candidate log (incl. explicitly deferred items & when to revisit) |
| [0004](docs/adr/0004-split-plan-dag.md) | Split the `plan_dag` god-module into planner / execute_reflect / orchestrator |
| [0005](docs/adr/0005-raw-requirement-passthrough.md) | Raw-requirement passthrough (executor/critic aligned to the user's original text) |
| [0006](docs/adr/0006-report-level-reviewer.md) | Report-level Reviewer (lower half of double-loop reflection, rewrite + targeted research) |
| [0007](docs/adr/0007-current-date-injection.md) | Centralized current-date injection (LLM seam, prevents time-sensitivity hallucination) |
| [0008](docs/adr/0008-external-config-toml.md) | Externalized behavior config (config.toml; keys vs behavior separated) |
| [0009](docs/adr/0009-citation-from-tool-layer.md) | Anchor citation sources at the search-tool layer (not LLM self-report) |

New domain terms are registered in the [`CONTEXT.md`](CONTEXT.md) glossary.

---

## Limitations & Roadmap

**Current limitations** (some are explicit YAGNI deferrals — see ADR-0003):

- **Single tool**: only `web_search` (the `execute_tool` callback already supports plugging in any tool).
- **No cross-session memory**: each REPL task is independent; no multi-turn context accumulation.
- **Report export**: only `report.md` is persisted; no format conversion / sharing.
- **No streaming in concurrent layers**: DAG same-layer concurrency uses echo=off (to avoid interleaving); single-node layers and the report stay streamed.
- **No route cache**: one classification call per task.
- **Not packaged**: not pip-installable; no `pyproject.toml`.
- **Logic-skewed test coverage**: the router/react/plan_execute execution paths have only real-API smoke tests, no offline tests.

**Possible next steps** (not commitments — pick as needed):

- Add `read_url` (read full page) / calculator / code-execution tools.
- Critic → Replan signal feedback (let a subtask Critic's "planner-level drift" finding directly trigger replanning).
- Migrate to pytest + CI.
- Cross-session memory / report export.

---

## License

[MIT](LICENSE). Copyright (c) 2026 **jidechao/AES(GuangDian)**.
