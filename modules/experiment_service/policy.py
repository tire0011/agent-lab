from __future__ import annotations

import json
import re
from pathlib import Path

from .models import ExperimentPlan

# Unambiguous reject keywords: any case-insensitive substring hit rejects
# immediately. Secret/credential terms are deliberately NOT here - they are handled by
# the action-based _SECRET_ACTION_REGEXES, since a bare mention ("without secrets",
# "tests the redaction logic for tokens") must not reject. "commit"/"push"/"merge" stay
# for the plan-level check (LLM plan JSON, any occurrence is a planned git op); the
# goal-level check uses the negation-aware _GIT_ACTION_REGEXES instead.
_REJECT_KEYWORDS: tuple[str, ...] = (
    "drop table", "delete from", "truncate table", "rm -rf",
    "shell=true",
    # Paid/costly actions: only the action words themselves, never bare nouns like
    # "budget" or "cost", so a plan's risk assessment mentioning cost does not reject.
    "purchas",  # stem: matches purchase/purchasing/purchased/purchases
    "enable billing", "paid tier", "spend money",
    "increase budget", "increase the budget", "credit card",
)

# Git-action keywords treated specially (word/context-aware) in _check_goal_policy.
_GIT_ACTION_KEYWORDS: frozenset[str] = frozenset({"commit", "push", "merge"})

# Action-based infra/deployment check: bare mentions of "VM"/"Azure"/"deploy" must NOT
# alone reject (the agent must be able to read, discuss and document such topics). Only
# an action verb within a word-count window of a target noun counts as describing a real
# change - proximity is word-count based, so an incidental noun in another sentence does
# not false-positive.
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

# Max filler words between verb and noun for the pair to count as one action
# ("delete the VM" = 1, "resize the VM to Standard_B2s" = 1); more words apart means
# not a match.
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

# Git-action detection for raw goal text: plain substring matching false-positives on
# safe mentions ("do not commit or push", "improve merge-conflict handling",
# "push-notification support"), so the goal check uses (a) explicit "git commit/push/merge"
# and (b) git verb + git-object proximity, both negation-aware via
# _infra_action_match_is_false_positive, so "do not commit", "without pushing",
# "won't merge" are accepted.
_GIT_OBJECT_NOUNS: tuple[str, ...] = (
    "change", "changes", "code", "branch", "branches",
    "result", "results", "implementation", "work",
    "pr", "prs", "pull request", "pull requests",
    "it", "them", "this", "that", "everything", "all",
)
_GIT_ACTION_MAX_FILLER = 4
_GIT_NOUN_ALT = "|".join(re.escape(n) for n in _GIT_OBJECT_NOUNS)
_GIT_ACTION_REGEXES: tuple[re.Pattern[str], ...] = (
    # (a) Explicit git sub-command: "git commit", "git push", "git merge".
    re.compile(r"\bgit\s+(?:commit|push|merge)\b", re.IGNORECASE),
    # (b) Git verb within a few words of a git-object noun: "commit the changes",
    #     "push the branch", "merge the PR".
    re.compile(
        rf"\b(?:commit|push|merge)\b(?:\s+\S+){{0,{_GIT_ACTION_MAX_FILLER}}}\s+"
        rf"\b(?:{_GIT_NOUN_ALT})\b",
        re.IGNORECASE,
    ),
)

# Words between a matched verb and noun that mean the plan is *disclaiming* the action
# ("does not touch infrastructure", "will not deploy anything"): any negation inside
# the matched span turns off that specific match. Absence words ("absent", "missing",
# "excluded") count as negation for the secret check only - "secrets are absent from
# public activity output" describes a lack, not an action.
_INFRA_ACTION_NEGATION_WORDS = frozenset(
    {
        "not", "no", "never", "without", "cannot", "avoid", "don't",
        "doesn't", "won't", "wouldn't", "shouldn't", "didn't", "isn't",
        "aren't", "n't",
        "absent", "missing", "excluded", "omitted",
    }
)


def _infra_action_match_is_false_positive(matched_text: str, preceding_text: str = "") -> bool:
    """True if *matched_text* should NOT count as describing a real action: it crosses
    a sentence boundary, or it - or the small window of text just before it,
    ``preceding_text`` - contains a negation. "without changing production" is a
    disclaimer even though "without" falls outside the match span itself.
    """
    if re.search(r"[.!?]\s", matched_text):
        return True
    words = re.findall(r"[a-z']+", matched_text.lower())
    if any(word in _INFRA_ACTION_NEGATION_WORDS or word.endswith("n't") for word in words):
        return True
    if preceding_text:
        # Look for a negation anywhere since the start of the current sentence/clause, not just
        # the preceding word: "without network access, secrets, or production changes" lists
        # several nouns after one leading negation, so it still applies to later items.
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


# Action-based secret/credential check: bare mentions of "secret"/"credential"/"token"/"api key" must NOT alone reject - legitimate plans discuss redaction, assert they need
# no secrets, or test secret-scrubbing. Only an action verb close to a target noun
# (read/obtain/use/copy/expose/store/transmit/create/change/delete a real secret)
# rejects. Same proximity, sentence-boundary and negation logic as the infra check, so
# "without secrets", "does not access credentials", "no API key required" pass.
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

# Generic "token" is overloaded (credential vs LLM accounting unit): keep
# credential-qualified tokens strict, exempt only explicit metric/tokenization
# phrases in the immediate context of a generic token match.
_LLM_TOKEN_METRIC_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:input|output|prompt|completion|context)\s+tokens?\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+counts?\b", re.IGNORECASE),
    re.compile(r"\bcount\s+tokens?\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+usage\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+budget\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+per\s+second\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+rate\b", re.IGNORECASE),
    # A quantity of tokens is a measure, not a credential (#348: "used more than a
    # million tokens").
    re.compile(r"\b(?:\d[\d,.]*\s*[km]?|hundred|thousand|million|billion)\s+tokens\b", re.IGNORECASE),
)
_CREDENTIAL_QUALIFIED_TOKEN_REGEX = re.compile(
    r"\b(?:access|auth|authentication|api|bearer|credential|secret|session|oauth)\s+tokens?\b",
    re.IGNORECASE,
)


_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (("`", "`"), ('"', '"'), ("“", "”"), ("'", "'"))


def _is_quoted(preceding_text: str, following_text: str) -> bool:
    """True when the match sits inside quotes on its own line (goal text only).

    Quoted text is a mention - typically a cited past rejection (#395) - not an action
    the goal asks for. An apostrophe inside a word ("doesn't") is not an opening quote.
    """
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
    """Return True only for negated or clearly LLM-token metric matches."""
    if _infra_action_match_is_false_positive(matched_text, preceding_text):
        return True

    # Metric exemptions apply only when the secret noun is the ambiguous generic
    # "token(s)", never api keys/passwords/etc.
    if not re.search(r"\btokens?\b", matched_text, re.IGNORECASE):
        return False

    context = f"{preceding_text[-120:]} {matched_text} {following_text[:120]}"
    if _CREDENTIAL_QUALIFIED_TOKEN_REGEX.search(context):
        return False
    return any(regex.search(context) for regex in _LLM_TOKEN_METRIC_REGEXES)


# Title phrases marking a placeholder/intention-only response rather than a concrete,
# already-selected feature.
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

# Documentation-only plans are not implementations: classify only obvious documentation
# files instead of maintaining a positive list of source extensions.
_DOCUMENTATION_ONLY_SUFFIXES: tuple[str, ...] = (".md", ".rst", ".txt")


def _is_code_or_test_file(path: str) -> bool:
    """Treat any concrete file that is not plain documentation as implementation material."""
    name = Path(path).name.lower()
    if not name or name in {"ideas.md", "readme", "readme.md"}:
        return False
    return not name.endswith(_DOCUMENTATION_ONLY_SUFFIXES)

def plan_violates_policy(plan: ExperimentPlan, raw_text: str) -> str | None:
    """Returns a human-readable rejection reason, or ``None`` if acceptable."""
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

    # Safety net against placeholder plans that satisfy the JSON schema but describe only
    # the intent to investigate; an additional conservative check - it never loosens any
    # existing rule.
    title_lower = plan.title.strip().lower()
    for phrase in _PLACEHOLDER_PHRASES:
        if phrase in title_lower:
            return f"plan title looks like a placeholder/intention-only statement: {plan.title!r}"

    # A plan whose affected_files are all documents (e.g. only IDEAS.md) is an investigation,
    # not an implementable feature: at least one concrete code or test file is required
    # (documentation alongside real code is still fine).
    if not any(_is_code_or_test_file(path) for path in plan.affected_files):
        return (
            "plan does not touch any concrete code or test file; "
            "investigation-only or documentation-only changes "
            f"(affected_files={plan.affected_files!r}) are not an "
            "implementable feature"
        )

    if len(plan.affected_files) > 1:
        # More than a small handful of affected files suggests more than "one small connected
        # feature" - conservative reject.
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


def _check_goal_policy(goal: str) -> str | None:
    """Apply the safety denylist directly to the raw goal text.

    Pre-implementation security check for the single-call pipeline (no LLM plan to
    scan). Returns a rejection reason or ``None``. Unlike the plan-level check,
    "commit"/"push"/"merge" are handled by the word-boundary, negation-aware
    _GIT_ACTION_REGEXES, so safe goals like "do not commit or push" are not rejected.
    """
    haystack = (goal or "").lower()
    # Git commit/push/merge are normal experiment-branch workflow actions; main/merge/# deployment governance is enforced by the lifecycle layer.
    for keyword in _REJECT_KEYWORDS:
        if keyword in haystack:
            return f"goal mentions disallowed keyword: {keyword!r}"
    return None


def _goal_secret_warning(goal: str) -> str | None:
    """Flag a goal that reads like a secret/credential action.

    Warning only, not a rejection: issues usually *describe* such actions (a leak to
    fix, a quoted rejection), which the regexes cannot tell apart. The reviewer checks
    the actual change before it is merged.
    """
    haystack = (goal or "").lower()
    for regex in _SECRET_ACTION_REGEXES:
        for match in regex.finditer(haystack):
            preceding = haystack[max(0, match.start() - 200):match.start()]
            following = haystack[match.end():min(len(haystack), match.end() + 120)]
            if _secret_action_match_is_false_positive(match.group(0), preceding, following):
                continue
            # Goal text only: a plan is checked as raw JSON, where every value is in quotes.
            if _is_quoted(preceding, following):
                continue
            return (
                "goal describes an actual secret/credential action: "
                f"{match.group(0)!r}"
            )
    return None


# Policy rule classification: map a free-text rejection reason to a short, fixed rule id.
# Never carries plan-derived free text (which could echo model output) - only a fixed
# vocabulary matched against the hard-coded reason prefixes, so it is always safe to
# log and expose over the API.
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
    """Classify a rejection reason into a short, fixed policy-rule id.

    Returns ``None`` when no known rule matches (e.g. an upstream worker error)
    rather than guessing.
    """
    if not reason:
        return None
    lowered = reason.lower()
    for prefix, rule in _POLICY_RULE_PREFIXES:
        if lowered.startswith(prefix):
            return rule
    return "policy_check_failed"
