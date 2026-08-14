---
type: idea
created_by: rene-and-chatgpt
---

Evaluate a Context Curator that selects a small evidence-backed set of relevant files, symbols, and line ranges before an expensive coding model receives a task.

Reuse existing open-source approaches before building a custom index. Compare at least: a simple rg plus Python/JavaScript symbol-outline baseline, Aider's Tree-sitter repository-map approach, and Serena's LSP/MCP symbol retrieval. Consider Zoekt only when repository size or multi-repository search justifies its additional service and index cost.

Give each curator a strict context budget, initially 4K or 8K tokens, and return file paths, relevant symbols or ranges, dependency clues, confidence, and reasons. Never include file contents merely because a file is large or central. Let the coding model request bounded context expansion when evidence is insufficient.

Evaluate on frozen representative tasks using relevant-file recall/precision, missing-required-file rate, context tokens, total task tokens, runtime, memory, implementation success, and test results. Compare against the current unconstrained exploration baseline, including the 1,102,844-token Logs task. Adopt only if quality remains stable while total tokens and latency improve.
