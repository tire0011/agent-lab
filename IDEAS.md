# IDEAS.md

An open, continuously extensible idea repository for agent-lab.

This is **not** a task list, a roadmap, or a commitment. An idea being
listed here does not mean it will be built, and its absence does not mean
it is rejected. Ideas exist to be explored, tested, adopted, or dropped as
evidence comes in.

For a record of what has actually been *built and verified*, see
`ARCHITECTURE.md` ("Current architecture (implemented)") and
`DECISIONS.md` (accepted/deferred decisions). This file is intentionally
forward-looking and speculative; that historical record is not.

## Anyone can add ideas, any time

Humans and agents (Nexus, Codex, Copilot, or others) may append a new idea
to this file at any time, in any section, without prior approval. New
ideas do not need a priority, an owner, or a fully worked-out plan — a
short description is enough. Keep each idea to one line so it stays easy
to scan and to parse; expand in prose elsewhere only once an idea moves
into `exploring` or later.

## States

Each idea may carry a lightweight state, written as a trailing
`` `state` `` marker. A state is optional — an idea with no marker is
implicitly `new`.

- `new` — captured, not yet looked at.
- `exploring` — actively being investigated or discussed, no commitment yet.
- `selected` — chosen as worth a concrete attempt, not yet built/tested.
- `tested` — a version exists and has been run/measured somehow.
- `adopted` — kept as part of the system based on evidence.
- `rejected` — tried or considered and deliberately not pursued (a reason
  is helpful but not required).

Ideas are not required to have a priority. Ordering within a section is
not a significance ranking.

## Ideas

### V1 / MCP agent

- Finish and test the MCP-based V1 agent path; define its scope (capabilities and limits) explicitly. `exploring`
- Add tests for MCP path success/failure cases. `new`
- Validate V1 behavior on both local machine and VM. `new`

### Benchmarking V0 vs V1

- Create a fixed benchmark task set with a quality rubric/scorer. `new`
- Track tokens, runtime, estimated cost, and resource usage per benchmark run. `new`
- Produce a side-by-side V0/V1 benchmark report. `new`

### Model/provider abstraction

- Define a provider/model interface; implement an Azure adapter first, then at least one non-Azure adapter. `new`
- Evaluate cheaper Azure model options and at least one additional provider for quality/cost/runtime tradeoff. `new`

### Evaluation gate before self-modification

- Define pass/fail criteria before any self-modification is allowed. `new`
- Block changes that fail evaluation; log outcomes in the structured store. `new`

### Controlled self-improvement loop

- Agent reviews previous runs and identifies one measurable weakness. `new`
- Agent proposes one bounded improvement before being allowed to modify itself automatically. `new`
- Evaluate each candidate against a fixed baseline; accept only changes that pass quality/cost/runtime gates, otherwise roll back. `new`
- Persist experiment hypothesis, change, metrics, result, decision, and cost. `new`
- Fix the experiment diff collector to also detect and include new, untracked files, so newly created files show up in the stored diff/diff_stat and are available for evaluation and reports. `new`

### Test isolation / second VM

- Investigate a second Azure VM as an ephemeral test worker; compare cost of local tests on the main VM vs a disposable test VM for risky changes. `new`
- Preferred flow if adopted: create/start test VM -> fresh checkout -> tests/benchmarks -> return result -> deallocate/delete. `new`
- Don't keep a second VM running 24/7 unless measurements justify it. `new`

### Coding CLI as a replaceable tool

- Let the orchestrator request a code change from a CLI, capture diff/test results, then independently evaluate the result. `exploring`
- Later compare multiple coding CLIs/models by task quality, cost, tokens, and runtime, and route tasks accordingly. `new`

### Codex task API follow-ups

- Add an approval workflow (currently any `queued` task can be run immediately by an authenticated caller). `new`
- Add task cancellation (currently a running task cannot be stopped once started). `new`
- Move execution off the request thread once background execution is deliberately in scope (currently strictly synchronous; a long Codex call blocks the HTTP response). `new`

### Baseline measurement

- Run one reproducible end-to-end baseline task and persist quality, tokens, runtime, estimated cost, and resource usage. `new`
- Keep the VM as the primary persistent home of the agent, repository, memory, orchestration, tests, and CLI tooling. `adopted`

### Web research worker

- Evaluate a self-hosted SearXNG instance as a low-cost search backend. `new`
- Build a small `WebResearchWorker` that searches through SearXNG, fetches a bounded number of result pages, and returns answers with source URLs. `new`
- Add strict request, page-count, download-size, timeout, and concurrency limits; respect robots.txt, provider terms, rate limits, and site access restrictions — do not scrape Google AI Overviews as a production dependency. `new`
- Log search count, fetched pages, tokens, runtime, failures, and estimated cost per research task. `new`
- Compare SearXNG against Gemini Grounding with Google Search on a small fixed question set for answer quality, source quality, freshness, runtime, and total cost. `new`
- When testing Gemini 3.x, account separately for the monthly included Google Search request allowance and the model input/output token charges — one prompt may trigger multiple search requests. `new`

### Memory and self model

- Add a persistent self model (`SELF.json` or SQLite-backed equivalent) containing version, capabilities, known weaknesses, resources, goals, and current constraints. `new`
- Separate memory into current task context, project memory, experience/experiment memory, and self model. `new`
- Load only relevant memory into LLM context instead of continuously growing one large prompt. `new`
- Reuse the existing SQLite store for structured memories and experiment history before adding embeddings/RAG. `new`

### Prompt library and work modes

- Add a versioned prompt library for distinct work modes such as `explore`, `research`, `plan`, `implement`, and `evaluate`. `new`
- Let the orchestrator select the appropriate prompt and permitted tools per task instead of assuming every task is a code change. `new`
- Allow the agent to propose, create, and improve its own prompts and add new work modes when a capability gap is identified. `new`
- Evaluate every prompt change against a baseline for quality, cost, tokens, and runtime before adoption; keep prompt history and automatically roll back regressions or constraint violations. `new`
- Store prompt versions, hypotheses, evaluations, and adoption decisions in SQLite. `new`

### Documentation accuracy

- Periodically re-check that README.md/AGENTS.md describe the actual deployed VM size, API endpoints, clients, and Web UI rather than a stale snapshot. `exploring`

### Communication interface

- Add experiment, cost, and approval views once the corresponding backend contracts exist. `new`
- Keep communication adapters separate so messenger integrations such as WhatsApp can be added later without changing the agent core. `new`
- Explore using the same communication contract for agent-to-agent communication. `exploring`

### Autonomy stages (framework idea, not a plan of record)

- Level 0: human assigns every task. `adopted`
- Level 1: agent proposes its own next experiment. `new`
- Level 2: agent executes cheap, reversible, bounded experiments automatically. `new`
- Level 3: continuous improvement loop with automatic evaluation and rollback. `new`
- Level 4: architecture/model/resource changes within explicit budgets and safety limits. `new`

### Later / no timeline

- Azure actual-cost reconciliation against estimated cost. `new`

## Open unknowns

Unknowns worth tracking without necessarily turning each into an idea above:

- Which evaluation strategy is robust enough for automated gating.
- Which low-cost model mix offers acceptable quality.
- How much autonomy is safe on current infrastructure limits.
- Which VM size gives the best cost/performance for coding CLI workloads.
- Whether an ephemeral second VM is cheaper and safer than testing on the main VM.
- Which memory retrieval strategy remains simple enough while still scaling with accumulated experience.
- Which stable Codex CLI output or event format exposes usage and session metadata reliably.
