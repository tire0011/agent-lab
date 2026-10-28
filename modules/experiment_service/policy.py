from __future__ import annotations

import json
import re
from pathlib import Path

from .models import ExperimentPlan

# unambiguous by themselves — the bare word already implies the disallowed
# action (there is no legitimate "harmless mention" of e.g. "pip install"
# or "drop table" inside a plan) — so any hit rejects the experiment
# immediately, regardless of surrounding context. Matching is a simple
# case-insensitive substring check against the lowercased haystack.
# Secret/credential-related terms are intentionally NOT here: they are
# handled by the action-based _SECRET_ACTION_REGEXES check below, since a
# bare mention (e.g. "without secrets", "tests the redaction logic for
# tokens") is a common, safe statement that must not reject a plan.
#
# NOTE: "commit", "push", and "merge" are kept here for the plan-level check
# (plan_violates_policy) where the haystack is LLM-generated plan JSON and
# any occurrence unambiguously means a planned git operation.  For the
# goal-level check (_check_goal_policy) these three verbs are handled by the
# word-boundary and negation-aware _GIT_ACTION_REGEXES instead — see the
# _GIT_ACTION_KEYWORDS frozenset below.
_REJECT_KEYWORDS: tuple[str, ...] = (
    "drop table", "delete from", "truncate table", "rm -rf",
    "shell=true",
    # Paid/costly actions: only the action words themselves (never bare
    # nouns like "budget" or "cost"), so mentioning cost considerations in
    # a plan's risk assessment does not accidentally reject it.
    "purchas",  # stem: matches purchase/purchasing/purchased/purchases
    "enable billing", "paid tier", "spend money",
    "increase budget", "increase the budget", "credit card",
)

# The three git-action keywords that are treated specially in
# _check_goal_policy (word/context-aware instead of plain substring).
_GIT_ACTION_KEYWORDS: frozenset[str] = frozenset({"commit", "push", "merge"})

# Action-based infrastructure/deployment check. Bare mentions of terms
# such as "VM", "Azure", "deploy", or "infrastructure" must NOT alone
# reject a plan (the agent needs to be able to read, discuss, and document
# such topics — e.g. fixing a VM version number in README.md). A plan is
# only rejected when one of these *action verbs* appears close to one of
# these *target nouns*, i.e. the plan actually describes creating,
# changing, deleting, or deploying real external infrastructure rather
# than just mentioning it. Proximity is word-count based (not a fixed
# character window) so an incidental mention of a noun in one sentence and
# an unrelated verb several sentences later does not trigger a false
# rejection.
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

# Maximum number of filler words allowed between the action verb and the
# target noun for the pair to still count as describing the same action
# (e.g. "delete the VM" -> 1 filler word; "resize the VM to Standard_B2s"
# -> 1 filler word). A plan that merely mentions "VM" in one sentence and
# an unrelated verb many words later is not considered a match.
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

# Git-action detection for the goal-level policy check (_check_goal_policy).
# The plan-level check (plan_violates_policy) keeps the simple substring
# matches in _REJECT_KEYWORDS because the haystack there is LLM-generated
# plan JSON and any occurrence of "commit" / "push" / "merge" is
# unambiguously a planned git operation.
#
# Raw user goals are different: they routinely contain safe incidental
# mentions that would be false-positively flagged by substring matching:
#   "do not commit or push"           (safety constraint / negation)
#   "improve merge-conflict handling" (incidental compound noun)
#   "handle commit message parsing"   (domain noun, not a git command)
#   "push-notification support"       (unrelated product domain)
#
# The word-boundary and negation-aware checks below are therefore used
# instead for raw goal text:
#   (a) Explicit git sub-command: "git commit / push / merge"  → always
#       an imperative git action; reject unless a negation precedes it.
#   (b) Git verb + git-object proximity: "commit the changes",
#       "push the branch", "merge the PR"  → reject unless negated.
#
# Both patterns reuse _infra_action_match_is_false_positive so that
# explicit negations ("do not commit", "without pushing", "won't merge")
# and clause-level negations ("never ..., commit ...") are accepted.
_GIT_OBJECT_NOUNS: tuple[str, ...] = (
    "change", "changes", "code", "branch", "branches",
    "result", "results", "implementation", "work",
    "pr", "prs", "pull request", "pull requests",
    "it", "them", "this", "that", "everything", "all",
)
_GIT_ACTION_MAX_FILLER = 4
_GIT_NOUN_ALT = "|".join(re.escape(n) for n in _GIT_OBJECT_NOUNS)
_GIT_ACTION_REGEXES: tuple[re.Pattern[str], ...] = (
    # (a) Explicit git sub-command: "git commit", "git push", "git merge"
    re.compile(r"\bgit\s+(?:commit|push|merge)\b", re.IGNORECASE),
    # (b) Git verb within a few words of a recognisable git-object noun:
    #     "commit the changes", "push the branch", "merge the PR"
    re.compile(
        rf"\b(?:commit|push|merge)\b(?:\s+\S+){{0,{_GIT_ACTION_MAX_FILLER}}}\s+"
        rf"\b(?:{_GIT_NOUN_ALT})\b",
        re.IGNORECASE,
    ),
)

# Words whose presence between a matched verb and noun indicate the plan is
# actually *disclaiming* the action ("does not touch infrastructure",
# "will not deploy anything") rather than describing it. A raw proximity
# match alone cannot tell "create a VM" apart from "will not create a VM",
# so any negation word found inside the matched span turns off the
# rejection for that specific match (the search continues past it).
#
# Also includes non-disclosure/absence state words ("absent", "missing",
# "excluded") for the secret/credential check specifically: defensive
# statements like "secrets are absent from public activity output" or
# "credentials are excluded from the response" describe the *lack* of a
# secret/credential rather than an action performed on one, and are easily
# confused with a real action match only because of an unrelated, later
# noun/verb-homograph word (e.g. "output") appearing further in the same
# sentence. Treating these as negation-equivalent state words keeps the
# fix narrowly scoped to negation/non-disclosure handling rather than
# adding a broad secret-domain keyword exception.
_INFRA_ACTION_NEGATION_WORDS = frozenset(
    {
        "not", "no", "never", "without", "cannot", "avoid", "don't",
        "doesn't", "won't", "wouldn't", "shouldn't", "didn't", "isn't",
        "aren't", "n't",
        "absent", "missing", "excluded", "omitted",
    }
)


def _infra_action_match_is_false_positive(matched_text: str, preceding_text: str = "") -> bool:
    """True if *matched_text* (a verb/noun proximity match) should NOT be
    treated as describing a real action — either because it crosses a
    sentence boundary (a later, unrelated sentence merely repeats the
    noun) or because it contains an explicit negation. ``preceding_text``
    (a small window of text immediately before the match) is also checked
    for a trailing negation word, so "without changing production" is
    recognized as a disclaimer even though "without" falls just outside
    the verb/noun match span itself."""
    if re.search(r"[.!?]\s", matched_text):
        return True
    words = re.findall(r"[a-z']+", matched_text.lower())
    if any(word in _INFRA_ACTION_NEGATION_WORDS or word.endswith("n't") for word in words):
        return True
    if preceding_text:
        # Look for a negation anywhere since the start of the current
        # sentence/clause, not just the immediately preceding word: prose
        # like "without network access, secrets, or production changes"
        # lists several nouns after a single leading negation, so a
        # negation several comma-separated items earlier still applies to
        # a later item in the same list.
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


# Action-based secret/credential check. Bare mentions of terms such as
# "secret", "credential", "token", "api key", or "ssh key" must NOT alone
# reject a plan — legitimate plans routinely discuss redaction logic,
# assert they need no secrets, or test secret-scrubbing code (e.g. this
# very module's own _redact_secrets tests). A plan is only rejected when
# one of these *action verbs* appears close to one of these *target
# nouns*, i.e. the plan actually describes reading, obtaining, using,
# copying, exposing/logging, storing, transmitting, creating, changing, or
# deleting a real secret/credential rather than just mentioning the
# concept. Reuses the same word-count proximity, sentence-boundary, and
# negation-guard logic as the infrastructure/deployment check above (see
# ``_infra_action_match_is_false_positive``), so "without secrets",
# "does not access credentials", and "no API key required" are accepted.
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

# Generic "token" is overloaded: it can mean a credential or an LLM
# accounting/tokenization unit.  Keep credential-qualified tokens strict,
# and exempt only explicit metric/tokenization phrases in the immediate
# context of a generic token match.
_LLM_TOKEN_METRIC_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:input|output|prompt|completion|context)\s+tokens?\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+counts?\b", re.IGNORECASE),
    re.compile(r"\bcount\s+tokens?\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+usage\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+budget\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+per\s+second\b", re.IGNORECASE),
    re.compile(r"\btokens?\s+rate\b", re.IGNORECASE),
    # A quantity of tokens is a measure, not a credential (#348: "used more
    # than a million tokens").
    re.compile(r"\b(?:\d[\d,.]*\s*[km]?|hundred|thousand|million|billion)\s+tokens\b", re.IGNORECASE),
)
_CREDENTIAL_QUALIFIED_TOKEN_REGEX = re.compile(
    r"\b(?:access|auth|authentication|api|bearer|credential|secret|session|oauth)\s+tokens?\b",
    re.IGNORECASE,
)


_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (("`", "`"), ('"', '"'), ("“", "”"), ("'", "'"))


def _is_quoted(preceding_text: str, following_text: str) -> bool:
    """True when the match sits inside quotes on its own line (goal text only).

    Quoted text is a mention - typically a past rejection reason cited in an
    issue (#395: "rejected ... for the same reason ('prints the actual api
    key')") - not an action the goal asks for. An apostrophe inside a word
    ("doesn't") is not an opening quote.
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

    # Metric exemptions apply only to matches whose secret noun is the
    # ambiguous generic token(s), never api keys/passwords/etc.
    if not re.search(r"\btokens?\b", matched_text, re.IGNORECASE):
        return False

    context = f"{preceding_text[-120:]} {matched_text} {following_text[:120]}"
    if _CREDENTIAL_QUALIFIED_TOKEN_REGEX.search(context):
        return False
    return any(regex.search(context) for regex in _LLM_TOKEN_METRIC_REGEXES)


# Title phrases that indicate a placeholder/intention-only response rather
# than a concrete, already-selected feature (e.g. the model announcing it
# is about to start investigating instead of reporting what it decided).
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

# Documentation-only plans are not implementations. Instead of maintaining a
# positive list of source extensions, classify only obvious documentation files.
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

    # Safety net against placeholder/intention-only plans that technically
    # satisfy the JSON schema (non-empty affected_files, all fields
    # present) but describe only the *intent* to investigate rather than a
    # concrete, implementation-ready feature. This is an additional,
    # conservative check — it never loosens any existing rule.
    title_lower = plan.title.strip().lower()
    for phrase in _PLACEHOLDER_PHRASES:
        if phrase in title_lower:
            return f"plan title looks like a placeholder/intention-only statement: {plan.title!r}"

    # A plan whose affected_files are all non-code/non-test documents (e.g.
    # only IDEAS.md) describes an investigation or documentation note, not
    # an implementable feature. This does not forbid legitimate
    # documentation *features* in general — it only requires that at least
    # one affected file be a concrete code or test file; a plan may still
    # additionally touch README.md/IDEAS.md/etc. alongside a real code file.
    if not any(_is_code_or_test_file(path) for path in plan.affected_files):
        return (
            "plan does not touch any concrete code or test file; "
            "investigation-only or documentation-only changes "
            f"(affected_files={plan.affected_files!r}) are not an "
            "implementable feature"
        )

    if len(plan.affected_files) > 1:
        # Not an automatic hard rejection by count alone, but more than a
        # small handful strongly suggests more than "one small connected
        # feature" — conservative reject.
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

    This is the pre-implementation security check for the single-call
    pipeline (no LLM-generated plan to scan).  Returns a human-readable
    rejection reason or ``None`` if the goal is acceptable.

    Unlike the plan-level check (plan_violates_policy), the git-action
    keywords "commit", "push", and "merge" are handled by the word-boundary
    and negation-aware _GIT_ACTION_REGEXES rather than the plain substring
    scan.  This avoids false-positive rejections of safe goals such as
    "do not commit or push", "improve merge-conflict handling", or
    "handle commit message parsing".
    """
    haystack = (goal or "").lower()
    # Git commit/push/merge are normal experiment-branch workflow actions.
    # Main/merge/deployment governance is enforced by the lifecycle layer.
    for keyword in _REJECT_KEYWORDS:
        if keyword in haystack:
            return f"goal mentions disallowed keyword: {keyword!r}"
    for regex in _SECRET_ACTION_REGEXES:
        for match in regex.finditer(haystack):
            preceding = haystack[max(0, match.start() - 200):match.start()]
            following = haystack[match.end():min(len(haystack), match.end() + 120)]
            if _secret_action_match_is_false_positive(match.group(0), preceding, following):
                continue
            # Goal text only: a plan is checked as raw JSON, where every value
            # is in quotes.
            if _is_quoted(preceding, following):
                continue
            return (
                "goal describes an actual secret/credential action: "
                f"{match.group(0)!r}"
            )
    return None


# ---------------------------------------------------------------------------
# Policy rule classification — turns a free-text rejection reason (produced
# by plan_violates_policy() or by the surrounding pipeline) into a short,
# stable rule identifier. This never carries any plan-derived free text
# (which could echo something from the model's own output); it is only a
# fixed vocabulary word matched against the *known, hard-coded* reason
# prefixes above, so it is always safe to log and to expose over the API.
# ---------------------------------------------------------------------------
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

    Returns ``None`` when the reason does not match any known rule (e.g. an
    upstream worker error message) rather than guessing.
    """
    if not reason:
        return None
    lowered = reason.lower()
    for prefix, rule in _POLICY_RULE_PREFIXES:
        if lowered.startswith(prefix):
            return rule
    return "policy_check_failed"
