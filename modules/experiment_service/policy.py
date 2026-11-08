from __future__ import annotations

import json
import re
from pathlib import Path

from .models import ExperimentPlan

# Secret terms need action-aware matching because safe plans may discuss redaction.
_REJECT_KEYWORDS: tuple[str, ...] = (
    "drop table", "delete from", "truncate table", "rm -rf",
    "shell=true",
    # Bare cost nouns occur in risk assessments; reject only costly actions.
    "purchas",
    "enable billing", "paid tier", "spend money",
    "increase budget", "increase the budget", "credit card",
)

_GIT_ACTION_KEYWORDS: frozenset[str] = frozenset({"commit", "push", "merge"})

# Bare infrastructure nouns are safe; a nearby action verb signals a real change.
_INFRA_ACTION_VERBS: tuple[str, ...] = (
    "create", "creates", "creating", "created",
    "provision", "provisions", "provisioning", "provisioned",
    "delete", "deletes", "deleting", "deleted",
    "destroy", "destroys", "destroying", "destroyed",
    "terminate", "terminates", "terminating", "terminated",
    "resize", "resizes", "resizing", "resized",
    "scale", "scales", "scaling", "scaled",
    "restart", "restarts", "restarting", "restarted",
    "stop", "stops", "stopping", "stopped",
    "start", "starts", "starting", "started",
    "modify", "modifies", "modifying", "modified",
    "configure", "configures", "configuring", "configured",
    "reconfigure", "reconfigures", "reconfiguring", "reconfigured",
    "change", "changes", "changing", "changed",
    "update", "updates", "updating", "updated",
    "install", "installs", "installing", "installed",
    "deploy", "deploys", "deploying", "deployed",
)

_INFRA_TARGET_NOUNS: tuple[str, ...] = (
    "vm", "virtual machine", "azure", "infrastructure", "nginx", "dns",
    "cloud resource", "resource group", "server", "subscription",
    "database instance", "storage account", "load balancer",
    "production", "prod", "environment", "website", "site",
)

_INFRA_ACTION_MAX_FILLER_WORDS = 6

_INFRA_VERB_ALT = "|".join(re.escape(v) for v in _INFRA_ACTION_VERBS)
_INFRA_NOUN_ALT = "|".join(re.escape(n) for n in _INFRA_TARGET_NOUNS)
_INFRA_ACTION_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"\b(?:{_INFRA_VERB_ALT})\b(?:\s+\S+){{0,{_INFRA_ACTION_MAX_FILLER_WORDS}}}\s+"
        rf"\b(?:{_INFRA_NOUN_ALT})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:{_INFRA_NOUN_ALT})\b(?:\s+\S+){{0,{_INFRA_ACTION_MAX_FILLER_WORDS}}}\s+"
        rf"\b(?:{_INFRA_VERB_ALT})\b",
        re.IGNORECASE,
    ),
)

# Proximity and negation avoid rejecting merge conflicts and "do not push".
_GIT_OBJECT_NOUNS: tuple[str, ...] = (
    "change", "changes", "code", "branch", "branches",
    "result", "results", "implementation", "work",
    "pr", "prs", "pull request", "pull requests",
    "it", "them", "this", "that", "everything", "all",
)
_GIT_ACTION_MAX_FILLER = 4
_GIT_NOUN_ALT = "|".join(re.escape(n) for n in _GIT_OBJECT_NOUNS)
_GIT_ACTION_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgit\s+(?:commit|push|merge)\b", re.IGNORECASE),
    re.compile(
        rf"\b(?:commit|push|merge)\b(?:\s+\S+){{0,{_GIT_ACTION_MAX_FILLER}}}\s+"
        rf"\b(?:{_GIT_NOUN_ALT})\b",
        re.IGNORECASE,
    ),
)

# Absence words also negate secret actions such as "secrets are absent".
_INFRA_ACTION_NEGATION_WORDS = frozenset(
    {
        "not", "no", "never", "without", "cannot", "avoid", "don't",
        "doesn't", "won't", "wouldn't", "shouldn't", "didn't", "isn't",
        "aren't", "n't",
        "absent", "missing", "excluded", "omitted",
    }
)


def _infra_action_match_is_false_positive(matched_text: str, preceding_text: str = "") -> bool:
    """Return whether sentence boundaries or negation disclaim the action."""
    if re.search(r"[.!?]\s", matched_text):
        return True
    words = re.findall(r"[a-z']+", matched_text.lower())
    if any(word in _INFRA_ACTION_NEGATION_WORDS or word.endswith("n't") for word in words):
        return True
    if preceding_text:
        # A leading negation applies across lists within the same clause.
        sentence_start = 0
        for boundary in re.finditer(r"[.!?]\s|\b(?:but|however|although|though)\b", preceding_text):
            sentence_start = boundary.end()
        clause = preceding_text[sentence_start:]
        clause_words = re.findall(r"[a-z']+", clause.lower())
        if any(
            word in _INFRA_ACTION_NEGATION_WORDS or word.endswith("n't")
            for word in clause_words
        ):
            return True
    return False


# Secret nouns require a nearby action; discussion and negated actions remain safe.
_SECRET_ACTION_VERBS: tuple[str, ...] = (
    "read", "reads", "reading",
    "obtain", "obtains", "obtaining", "obtained",
    "fetch", "fetches", "fetching", "fetched",
    "retrieve", "retrieves", "retrieving", "retrieved",
    "use", "uses", "using", "used",
    "load", "loads", "loading", "loaded",
    "copy", "copies", "copying", "copied",
    "expose", "exposes", "exposing", "exposed",
    "leak", "leaks", "leaking", "leaked",
    "print", "prints", "printing", "printed",
    "log", "logs", "logging", "logged",
    "output", "outputs", "outputting",
    "store", "stores", "storing", "stored",
    "save", "saves", "saving", "saved",
    "transmit", "transmits", "transmitting", "transmitted",
    "send", "sends", "sending", "sent",
    "upload", "uploads", "uploading", "uploaded",
    "create", "creates", "creating", "created",
    "generate", "generates", "generating", "generated",
    "change", "changes", "changing", "changed",
    "rotate", "rotates", "rotating", "rotated",
    "delete", "deletes", "deleting", "deleted",
    "remove", "removes", "removing", "removed",
)

_SECRET_ACTION_NOUNS: tuple[str, ...] = (
    "secret", "secrets", "credential", "credentials",
    "api key", "api keys", "api_key", "apikey",
    "ssh key", "ssh keys", "private key", "private keys",
    "access token", "access tokens", "auth token", "auth tokens",
    "token", "tokens", "password", "passwords",
)

_SECRET_ACTION_MAX_FILLER_WORDS = 6

_SECRET_VERB_ALT = "|".join(re.escape(v) for v in _SECRET_ACTION_VERBS)
_SECRET_NOUN_ALT = "|".join(re.escape(n) for n in _SECRET_ACTION_NOUNS)
_SECRET_ACTION_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"\b(?:{_SECRET_VERB_ALT})\b(?:\s+\S+){{0,{_SECRET_ACTION_MAX_FILLER_WORDS}}}\s+"
        rf"\b(?:{_SECRET_NOUN_ALT})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:{_SECRET_NOUN_ALT})\b(?:\s+\S+){{0,{_SECRET_ACTION_MAX_FILLER_WORDS}}}\s+"
        rf"\b(?:{_SECRET_VERB_ALT})\b",
        re.IGNORECASE,
    ),
)

# Generic tokens may be LLM metrics; credential-qualified tokens stay strict.
_LLM_TOKEN_METRIC_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:input|output|prompt|completion|context)\s+tokens?\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+counts?\b", re.IGNORECASE),
    re.compile(r"\bcount\s+tokens?\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+usage\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+budget\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+per\s+second\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+rate\b", re.IGNORECASE),
    re.compile(r"\b(?:\d[\d,.]*\s*[km]?|hundred|thousand|million|billion)\s+tokens\b", re.IGNORECASE),
)
_CREDENTIAL_QUALIFIED_TOKEN_REGEX = re.compile(
    r"\b(?:access|auth|authentication|api|bearer|credential|secret|session|oauth)\s+tokens?\b",
    re.IGNORECASE,
)


_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (("`", "`"), ('"', '"'), ("“", "”"), ("'", "'"))


def _is_quoted(preceding_text: str, following_text: str) -> bool:
    """Return whether a goal match is quoted on its line, excluding apostrophes."""
    before = preceding_text.rsplit("\n", 1)[-1]
    after = following_text.split("\n", 1)[0]
    for opening, closing in _QUOTE_PAIRS:
        start = before.rfind(opening)
        if start < 0 or closing not in after:
            continue
        if opening == closing and before.count(opening) % 2 == 0:
            continue
        if opening == "'" and start > 0 and before[start - 1].isalnum():
            continue
        return True
    return False


def _secret_action_match_is_false_positive(
    matched_text: str,
    preceding_text: str = "",
    following_text: str = "",
) -> bool:
    """Return whether a secret-action match is negated or an LLM token metric."""
    if _infra_action_match_is_false_positive(matched_text, preceding_text):
        return True

    # Metric exemptions apply only to generic token(s), never other credentials.
    if not re.search(r"\btokens?\b", matched_text, re.IGNORECASE):
        return False

    context = f"{preceding_text[-120:]} {matched_text} {following_text[:120]}"
    if _CREDENTIAL_QUALIFIED_TOKEN_REGEX.search(context):
        return False
    return any(regex.search(context) for regex in _LLM_TOKEN_METRIC_REGEXES)


_PLACEHOLDER_PHRASES: tuple[str, ...] = (
    "inspection started",
    "analysis only",
    "investigation started",
    "investigating",
    "will inspect",
    "will investigate",
    "will analyze",
    "read-only analysis",
    "to be determined",
    "tbd",
    "placeholder",
    "repository inspection",
    "repo inspection",
    "code review only",
    "documentation only",
)

# Avoid a brittle positive list of source extensions.
_DOCUMENTATION_ONLY_SUFFIXES: tuple[str, ...] = (".md", ".rst", ".txt")


def _is_code_or_test_file(path: str) -> bool:
    """Return whether a concrete path may contain implementation material."""
    name = Path(path).name.lower()
    if not name or name in {"ideas.md", "readme", "readme.md"}:
        return False
    return not name.endswith(_DOCUMENTATION_ONLY_SUFFIXES)

def plan_violates_policy(plan: ExperimentPlan, raw_text: str) -> str | None:
    """Return a human-readable plan rejection reason, if any."""
    haystack = (raw_text + "\n" + json.dumps(plan.to_dict())).lower()
    for keyword in _REJECT_KEYWORDS:
        if keyword in haystack:
            return f"plan mentions disallowed keyword: {keyword!r}"


    for regex in _SECRET_ACTION_REGEXES:
        for match in regex.finditer(haystack):
            preceding = haystack[max(0, match.start() - 200):match.start()]
            following = haystack[match.end():min(len(haystack), match.end() + 120)]
            if _secret_action_match_is_false_positive(match.group(0), preceding, following):
                continue
            return (
                "plan describes an actual secret/credential action: "
                f"{match.group(0)!r}"
            )

    title_lower = plan.title.strip().lower()
    for phrase in _PLACEHOLDER_PHRASES:
        if phrase in title_lower:
            return f"plan title looks like a placeholder/intention-only statement: {plan.title!r}"

    # Workspace-write plans require code or tests, though they may include docs.
    if not any(_is_code_or_test_file(path) for path in plan.affected_files):
        return (
            "plan does not touch any concrete code or test file; "
            "investigation-only or documentation-only changes "
            f"(affected_files={plan.affected_files!r}) are not an "
            "implementable feature"
        )

    if len(plan.affected_files) > 1:
        if len(plan.affected_files) > 4:
            return (
                f"plan touches too many files ({len(plan.affected_files)}); "
                "must be one small, connected feature"
            )

    for path in plan.affected_files:
        if path.startswith("/") and not path.startswith("/agent"):
            return f"plan references a path outside the repository: {path!r}"
        if ".." in Path(path).parts:
            return f"plan references a path outside the repository: {path!r}"

    return None


_HANDOVER_SECTION_RE = re.compile(
    r"\n\n(?:Knowledge briefing:|Delegate findings \(unverified\):|Earlier attempts in this run:)"
)


def _policy_text(goal: str) -> str:
    """Exclude quoted handover evidence from goal policy checks (#544)."""
    from modules.experiment_handover import EARLIER_ATTEMPTS_HEADING, HANDOVER_HEADING

    task = goal.split(HANDOVER_HEADING, 1)[0].split(EARLIER_ATTEMPTS_HEADING, 1)[0]
    _, found, handover = goal.partition(HANDOVER_HEADING)
    if not found:
        return task
    _, has_goal, nexus_goal = handover.partition("Nexus goal:\n")
    if not has_goal:
        return task
    return task + "\n" + _HANDOVER_SECTION_RE.split(nexus_goal, 1)[0].split(EARLIER_ATTEMPTS_HEADING, 1)[0]


def _check_goal_policy(goal: str) -> str | None:
    """Return a denylist rejection for the actionable part of a raw goal."""
    haystack = _policy_text(goal or "").lower()
    for keyword in _REJECT_KEYWORDS:
        if keyword in haystack:
            return f"goal mentions disallowed keyword: {keyword!r}"
    return None


def _goal_secret_warning(goal: str) -> str | None:
    """Warn, without rejecting, when a goal resembles a credential action."""
    haystack = (goal or "").lower()
    for regex in _SECRET_ACTION_REGEXES:
        for match in regex.finditer(haystack):
            preceding = haystack[max(0, match.start() - 200):match.start()]
            following = haystack[match.end():min(len(haystack), match.end() + 120)]
            if _secret_action_match_is_false_positive(match.group(0), preceding, following):
                continue
            if _is_quoted(preceding, following):
                continue
            return (
                "goal describes an actual secret/credential action: "
                f"{match.group(0)!r}"
            )
    return None


# Fixed IDs prevent plan-derived text from reaching logs or the API.
_POLICY_RULE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("plan output could not be reliably parsed", "unparseable_plan"),
    ("policy replan output could not be reliably parsed", "unparseable_plan"),
    ("agent found no suitable improvement", "no_suitable_improvement"),
    ("agent found no suitable alternative improvement", "no_suitable_improvement"),
    ("plan mentions disallowed keyword", "disallowed_keyword"),
    ("goal mentions disallowed keyword", "disallowed_keyword"),
    ("plan describes an actual secret/credential action", "secret_action"),
    ("goal describes an actual secret/credential action", "secret_action"),
    ("plan title looks like a placeholder/intention-only statement", "placeholder_title"),
    ("plan does not touch any concrete code or test file", "documentation_only"),
    ("plan touches too many files", "too_many_files"),
    ("plan references a path outside the repository", "path_outside_repo"),
    ("plan declares an unknown change_kind", "invalid_change_kind"),
    (
        "plan declares change_kind='tests_only' but affected_files names non-test",
        "tests_only_inconsistent",
    ),
    (
        "plan declares change_kind='tests_only' but the original task did not explicitly",
        "tests_only_not_justified",
    ),
    ("plan declares change_kind=", "insufficient_plan_artifacts"),
    ("research phase timed out", "research_timed_out"),
    ("plan phase timed out", "plan_timed_out"),
    ("policy replan failed", "policy_replan_failed"),
)


def _policy_rule_for_reason(reason: str | None) -> str | None:
    """Map a rejection reason to a fixed policy ID without echoing its text."""
    if not reason:
        return None
    lowered = reason.lower()
    for prefix, rule in _POLICY_RULE_PREFIXES:
        if lowered.startswith(prefix):
            return rule
    return "policy_check_failed"
