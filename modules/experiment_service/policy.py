"""Experiment policy facade.

Normal Git commit/push/merge operations on an experiment branch are part of the
Nexus development workflow. They are therefore not safety-policy keywords.
Main-branch, merge and deployment constraints are enforced by the lifecycle
and governance layers instead.

The remaining policy implementation stays in ``policy_legacy`` for now so this
hotfix changes only the obsolete Git deny rules and leaves infrastructure,
secret, destructive-command and cost protections unchanged.
"""

from . import policy_legacy as _policy

# Remove the obsolete plan-level keyword deny rules.
_policy._REJECT_KEYWORDS = tuple(
    keyword
    for keyword in _policy._REJECT_KEYWORDS
    if keyword not in {"commit", "push", "merge"}
)

# Remove the obsolete goal-level Git action matcher.  The legacy goal checker
# still applies all non-Git keyword, infrastructure and credential rules.
_policy._GIT_ACTION_REGEXES = ()

# Preserve the policy module's existing public/internal API while applying the
# narrowed runtime configuration above.
for _name in dir(_policy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_policy, _name)
