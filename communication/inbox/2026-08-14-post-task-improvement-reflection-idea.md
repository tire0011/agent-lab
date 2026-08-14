---
type: idea
created_by: rene-and-chatgpt
---

After every completed, failed, rejected, or timed-out task, run a bounded post-task reflection that asks an LLM what could improve Nexus's process, tools, prompts, context selection, tests, safety, cost, runtime, or architecture.

Give the reflection only a compact structured summary of the goal, plan, changed files, lifecycle timings, token usage, test outcome, failure evidence, and final decision; do not resend the full conversation, repository, prompts, diffs, or raw logs. Require at most three concrete evidence-backed suggestions, each with expected benefit, cost, risk, measurement method, and smallest experiment. Allow "no useful improvement found" as a valid result.

Use a strict small token budget and preferably the cheap local decision model when available. Deduplicate suggestions against existing ideas and previous reflections. Store new candidates with provenance and measurable hypotheses, but never implement them automatically merely because the reflecting model suggested them. Track which suggestions are later tested, adopted, rejected, or repeatedly rediscovered so reflection quality itself can be evaluated.
