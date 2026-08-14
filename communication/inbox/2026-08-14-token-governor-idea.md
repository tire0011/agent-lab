---
type: idea
created_by: rene-and-chatgpt
---

Add a Token Governor that enforces explicit per-phase and per-experiment budgets instead of merely reporting token usage after completion.

Define task-size classes and initial configurable budgets for research, selection, implementation, retries, and total usage. Before each LLM call, check remaining budget. When a soft limit is reached, require context compaction, narrower file retrieval, a cheaper model, or a revised smaller plan. At the hard limit, stop safely with needs_review rather than continuing uncontrolled.

Record tokens by model, call, phase, cached/uncached category when available, and useful output such as accepted changed lines or successful tests. Expose budget, consumed amount, remaining amount, and stop reason in logs and the Web UI. Start in shadow mode to learn realistic limits, then enforce limits for small tasks. Use the million-token Logs experiments as baseline evidence.
