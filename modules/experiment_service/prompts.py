from __future__ import annotations

from .models import ExperimentPlan

_NEXUS_IDENTITY_PROMPT = """You are operating as a reasoning and execution worker for Nexus, the persistent agent system. This task originates from Nexus unless explicitly marked as a human instruction. Refer to the acting system as "Nexus", not as "the user". You are not independently Nexus; you are temporarily working on Nexus's behalf."""

def _with_nexus_identity(prompt: str) -> str:
    return f"{_NEXUS_IDENTITY_PROMPT}\n\n{prompt}"

# ---------------------------------------------------------------------------
# Research prompt template (read-only investigation, free-form output)
# ---------------------------------------------------------------------------

_RESEARCH_PROMPT_TEMPLATE = """You are researching this repository to find candidate improvements. This is
a READ-ONLY investigation phase. Do not modify any files.

Goal: {goal}

Steps:
1. Inspect IDEAS.md and the repository structure using read-only tools
   (read files, search, list directories) to understand what small,
   low-risk, testable improvements are plausible.
2. Consider one or more candidate features or fixes that:
   - can be fully implemented and tested within 20 minutes,
   - touch at most a small handful of closely related files,
   - need no new dependency, no package installation,
   - need no infrastructure, cloud, or deployment action,
   - need no secrets or credentials,
   - are not an authentication or security relaxation,
   - do not commit, push, or merge anything,
   - prefer existing small IDEAS.md entries, tests, observability, or
     internal developer-experience improvements.
3. Write a concise, factual research summary (plain text, NOT JSON) that
   states which candidate(s) you considered, the concrete evidence you
   found for each (e.g. exact IDEAS.md line, existing file/test names), and
   which single candidate you recommend as the best next feature and why.

Do NOT emit a final structured plan or any JSON object in this phase — a
separate phase will do that using your research summary as input. Your
entire response should be the research summary itself: prose, not JSON,
and no code fence.
"""


def _build_research_prompt(goal: str) -> str:
    return _with_nexus_identity(_RESEARCH_PROMPT_TEMPLATE.format(goal=goal))


# ---------------------------------------------------------------------------
# Plan prompt template (no tools, schema-constrained output only)
# ---------------------------------------------------------------------------

_PLAN_PROMPT_TEMPLATE = """You are selecting exactly ONE small, low-risk, testable improvement for this repository, based ONLY on the research summary below.

Goal: {goal}

You have NO tools available in this phase and cannot run any command or
investigate the repository further. Base your entire decision solely on
the research summary provided below; do not assume anything beyond it.

Research summary:
---
{research_summary}
---

The final answer must describe one concrete, implementation-ready feature
and "affected_files" must contain at least one existing or planned
repository file. Do not return progress, placeholder, or intention-only
content (for example: "inspection started", "analysis only",
"investigating", or any other statement that merely describes what you are
about to do rather than what you have already decided).

Select exactly one small feature or fix from the research summary that:
- can be fully implemented and tested within 20 minutes,
- touches at most a small handful of closely related files,
- needs no new dependency, no package installation,
- needs no infrastructure, Azure, or deployment change,
- needs no secrets or credentials,
- is not an authentication or security relaxation,
- does not commit, push, or merge anything.

Produce your final response now. Your ENTIRE response MUST be exactly one
JSON object and NOTHING else: no prose before or after it, no markdown
code fence, no explanation outside the JSON. The object must have exactly
these keys: "title", "motivation", "affected_files" (list of repo-relative
paths), "expected_changes", "success_criteria", "planned_tests", "risks",
"estimated_scope", "change_kind", "required_artifacts",
"acceptance_criteria". All values must be strings, except "affected_files"
which must be a non-empty list of strings naming at least one existing or
planned repository file — never an empty list.

"change_kind" must be exactly one of: "implementation" (default for any
ordinary feature/fix/refactoring change), "tests_only" (ONLY if the goal
above explicitly and exclusively asks for a tests-only change with no
production-code modification — never choose this for a refactoring or
feature request), "documentation", or "analysis". "required_artifacts"
must be a list of the concrete new or modified non-test file paths that
must exist once the change is implemented (may be empty only for
"analysis"). "acceptance_criteria" must be a non-empty list of short,
measurable, checkable statements (e.g. "static/logs.js exists and is
served", not vague statements like "code is improved").

If no suitable small improvement exists in the research summary, respond
with a JSON object where "title" is exactly "NONE" and briefly explain why
in "motivation" (still exactly one JSON object and nothing else, with
"change_kind" set to "analysis" and both list fields empty).
"""


def _build_plan_prompt(goal: str, research_summary: str) -> str:
    return _with_nexus_identity(
        _PLAN_PROMPT_TEMPLATE.format(goal=goal, research_summary=research_summary)
    )


_REFORMAT_RETRY_PROMPT_TEMPLATE = """Reformat the previous plan as exactly one JSON object matching this schema. Do not select or modify the feature. Output JSON only.

Schema keys (all string values, except "affected_files",
"required_artifacts", and "acceptance_criteria" which are lists of
strings):
"title", "motivation", "affected_files", "expected_changes",
"success_criteria", "planned_tests", "risks", "estimated_scope",
"change_kind" (one of "implementation", "tests_only", "documentation",
"analysis"), "required_artifacts", "acceptance_criteria". If the previous
plan did not specify "change_kind", "required_artifacts", or
"acceptance_criteria", infer them conservatively from the previous plan's
own content (do not invent a new feature) — use "implementation" as the
default change_kind if genuinely unclear.

Previous plan (reformat this content only, do not change its meaning or
select a different feature):
---
{previous_output}
---

Your entire response must be exactly one JSON object and nothing else: no
prose, no markdown code fence, no explanation outside the JSON.
"""


def _build_reformat_retry_prompt(previous_output: str) -> str:
    return _with_nexus_identity(
        _REFORMAT_RETRY_PROMPT_TEMPLATE.format(previous_output=previous_output)
    )


# ---------------------------------------------------------------------------
# Policy-replan prompt template (separate from the parser reformat retry):
# used exactly once when a syntactically valid, schema-conformant plan was
# rejected by plan_violates_policy(). No tools, no further research; the
# research summary is re-supplied as-is, plus the rejected plan and the
# exact rejection reason, and the model is instructed to select a
# different, concrete code/test/config change instead.
# ---------------------------------------------------------------------------

_POLICY_REPLAN_PROMPT_TEMPLATE = """Your previous plan was rejected by an automated policy check. You must now select a DIFFERENT small, low-risk, testable improvement, based ONLY on the research summary below.

Goal: {goal}

You have NO tools available in this phase and cannot run any command or
investigate the repository further. Base your entire decision solely on
the research summary provided below; do not assume anything beyond it.

Research summary:
---
{research_summary}
---

Your previous (rejected) plan:
---
{rejected_plan_json}
---

Rejection reason (must not recur in your new plan):
---
{rejection_reason}
---

Select a genuinely different feature or fix that concretely changes at
least one existing or planned code, test, or configuration file (not only
documentation), and that avoids the exact problem described in the
rejection reason above. It must still:
- be fully implementable and testable within 20 minutes,
- touch at most a small handful of closely related files,
- need no new dependency, no package installation,
- need no infrastructure, Azure, or deployment change,
- need no secrets or credentials,
- not be an authentication or security relaxation,
- not commit, push, or merge anything.

Do not return progress, placeholder, or intention-only content (for
example: "inspection started", "analysis only", "investigating", or any
other statement that merely describes what you are about to do rather
than what you have already decided).

Produce your final response now. Your ENTIRE response MUST be exactly one
JSON object and NOTHING else: no prose before or after it, no markdown
code fence, no explanation outside the JSON. The object must have exactly
these keys: "title", "motivation", "affected_files" (list of repo-relative
paths), "expected_changes", "success_criteria", "planned_tests", "risks",
"estimated_scope", "change_kind", "required_artifacts",
"acceptance_criteria". All values must be strings, except "affected_files"
which must be a non-empty list of strings naming at least one existing or
planned repository file — never an empty list.

"change_kind" must be exactly one of: "implementation", "tests_only" (ONLY
if the goal explicitly and exclusively asks for a tests-only change),
"documentation", or "analysis". "required_artifacts" must list the
concrete new or modified non-test file paths that must exist once
implemented. "acceptance_criteria" must be a non-empty list of short,
measurable, checkable statements.

If no suitable alternative improvement exists, respond with a JSON object
where "title" is exactly "NONE" and briefly explain why in "motivation"
(still exactly one JSON object and nothing else, with "change_kind" set
to "analysis" and both list fields empty).
"""


def _build_policy_replan_prompt(
    goal: str, research_summary: str, rejected_plan_json: str, rejection_reason: str
) -> str:
    return _with_nexus_identity(
        _POLICY_REPLAN_PROMPT_TEMPLATE.format(
            goal=goal,
            research_summary=research_summary,
            rejected_plan_json=rejected_plan_json,
            rejection_reason=rejection_reason,
        )
    )


_IMPLEMENTATION_PROMPT_TEMPLATE = """Implement exactly the following pre-approved, small feature plan. Do not
implement anything beyond this plan.

Title: {title}
Motivation: {motivation}
Change kind: {change_kind}
Affected files: {affected_files}
Required artifacts (must exist on disk when you are done): {required_artifacts}
Expected changes: {expected_changes}
Success criteria: {success_criteria}
Acceptance criteria: {acceptance_criteria}
Planned tests: {planned_tests}

Allowed boundaries (must not be violated):
- Only modify files inside this working directory.
- Do not install any package or dependency.
- Do not change infrastructure, deployment, or Azure configuration.
- Do not read or output secrets or credentials.
- Do NOT run `git commit`, `git push`, or any merge.

Instructions:
1. Implement the change described above. If "Change kind" is
   "implementation" or "documentation", you MUST actually create/modify
   every file listed under "Required artifacts" — adding or editing tests
   alone does not satisfy this plan, even if those tests already pass.
2. Add or update tests so the success criteria are verifiable.
3. Do not commit or push. Leave the working tree with your changes unstaged.
"""


def _build_implementation_prompt(plan: ExperimentPlan) -> str:
    return _with_nexus_identity(
        _IMPLEMENTATION_PROMPT_TEMPLATE.format(
            title=plan.title,
            motivation=plan.motivation,
            change_kind=plan.change_kind,
            affected_files=", ".join(plan.affected_files),
            required_artifacts=", ".join(plan.required_artifacts) or "(none declared)",
            expected_changes=plan.expected_changes,
            success_criteria=plan.success_criteria,
            acceptance_criteria=", ".join(plan.acceptance_criteria) or "(none declared)",
            planned_tests=plan.planned_tests,
        )
    )


_DIRECT_IMPLEMENTATION_PROMPT_TEMPLATE = """Implement the following task in this repository.

Task: {goal}

Instructions:
1. Investigate the repository to understand the relevant code (read files,
   search, list directories).
2. Plan your implementation based on what you find.
3. Implement the change described in the task. If the task requires
   creating or modifying non-test source files, you MUST do so — adding
   or editing tests alone is not sufficient.
4. Add or update tests so the change is verifiable.
5. Do not commit or push. Leave the working tree with your changes
   unstaged.

Allowed boundaries (must not be violated):
- Only modify files inside this working directory.
- Do not install any package or dependency.
- Do not change infrastructure, deployment, or Azure configuration.
- Do not read or output secrets or credentials.
- Do NOT run `git commit`, `git push`, or any merge.
"""


def _build_direct_implementation_prompt(goal: str) -> str:
    """Build the single Codex implementation prompt directly from the task goal."""
    return _with_nexus_identity(
        _DIRECT_IMPLEMENTATION_PROMPT_TEMPLATE.replace("{goal}", goal)
    )
