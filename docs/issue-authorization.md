# Issue authorization architecture

How Nexus decides which GitHub issues it may execute autonomously while
`agent-lab` is public and the Nexus instance stays under owner control.

## The model

```
public GitHub (agent-lab)          private mailbox (agent-lab-mailbox)      local SQLite               Nexus
─────────────────────────          ─────────────────────────────────      ─────────────              ─────
issues, comments, PRs,      ──►    inbox/*.md messages, pushed        ──►   issue_approvals table  ──►  planner selects among
webhooks = discovery,              by trusted actors (git-provenance        (persistent approval           approved issues and
proposals, discussion              verified, HMAC webhook)                 registry)                      executes them
only - never an execution                                                                                   |
channel                                                                                          revocation cancels
                                                                                                     running work
```

- **Public GitHub = discovery, proposals and discussion.** Nexus lists all open
  issues every tick, and anyone may open issues, comment, or open PRs. None of
  that can trigger execution.
- **Private mailbox = trusted approval commands.** Structured
  `issue_approval` messages, accepted only when the git commit that added the
  inbox file belongs to a trusted actor (`modules/github_authorization.py`;
  the `created_by` front matter is display-only and never proof of identity).
- **Local SQLite = persistent authorization state.** The `issue_approvals`
  table (`modules/issue_approvals.py`) holds one row per
  `(repository, issue_number)`: status (`approved` / `revoked` / `rejected`),
  the approved content hash, timestamps, and the source mailbox message id for
  auditability. Created automatically by `init_db`; no manual migration.
- **Nexus = autonomous planning and execution of approved issues.** The
  initiative tick discovers all open issues, but only issues passing the
  registry check become candidates; the check is repeated at the dispatch
  boundary, and the worker receives the validated snapshot (title/body) so an
  edit between validation and execution cannot change what runs.

## Enforcement points

1. **Candidate scan** (`NexusInitiativeService._issue_candidates`): every open
   issue is checked with `issue_approvals.check_execution` (status must be
   `approved` and the content hash must match the current title+body).
   Failures are logged and recorded as `skipped_unapproved_issue`; only
   eligible issues reach the planner, which prioritizes independently.
2. **Dispatch boundary** (`NexusInitiativeService.tick`): the chosen issue is
   re-verified immediately before `controller.handle_issue`; the validated
   title/body snapshot is what the worker receives.
3. **Revocation sweep** (`NexusInitiativeService.enforce_issue_approvals`, run
   each tick): running experiments whose issue has an explicit
   `revoked`/`rejected` record are cancelled via
   `ExperimentService.cancel_experiment`. A merely pending (never approved)
   issue never cancels in-flight work, so legacy retries and resumptions are
   unaffected.
4. **Mailbox provenance** (`modules/mailbox/processing.py`): every inbox file
   is authorized by the git author of the commit that added it; untrusted
   senders get `pending_approval` tasks (never run), no operation chains, no
   `setting`/`refresh` changes, no `issue_approval` effect, and ideas stored
   for review instead of entering IDEAS.md as `new`.
5. **Webhook origin** (`POST /mailbox/webhook/github`): HMAC-SHA256 verified,
   and with `AGENT_MAILBOX_REPO=owner/mailbox-repo` set, payloads from any
   other repository are ignored.
6. **API/MCP**: all execution endpoints require `AGENT_API_KEY` (the
   intentional direct administrative path); the `/mcp` mount enforces the same
   key via middleware.

## What cannot authorize execution

Issue/PR/comment text (including "approved by the owner" claims), labels, PR
reviews, webhook payloads without a valid signature, forged `created_by`
front matter, and any content from a non-trusted mailbox commit. Author
identity is only ever taken from GitHub API actor fields (`user.login`) or the
git commit provenance - never from embedded text. Missing or corrupted
approval records fail closed.

## Approving or revoking an issue (ChatGPT, Claude, or a human)

Push a file into `inbox/` of the private `tire0011/agent-lab-mailbox`
repository (the GitHub webhook triggers processing immediately; the systemd
timer is the fallback):

```yaml
---
type: issue_approval
repository: tire0011/agent-lab
issue: 501
decision: approved        # approved | revoked | rejected
created_by: chatgpt
---
```

- `decision: approved` fetches the issue, binds the approval to a sha256 hash
  of its current title+body, and records it transactionally with the mailbox
  message id for audit. Confirmation (or the error) appears in `outbox/`.
- `decision: revoked` (or `rejected`) blocks new work and cancels running
  experiments on that issue at the next tick.
- Duplicate messages are idempotent (same decision + hash + source message =
  no change).
- Editing the issue title or body invalidates the approval: the content hash
  no longer matches, the issue becomes pending again and needs renewed
  approval.
- No shell execution is involved; the message is structured front matter, not
  a natural-language command.

To approve several issues, push one file per issue. To see which open issues
are still pending, compare `gh issue list` against the `issue_approvals` table
(`sqlite3 /var/lib/nexus/agent.db 'select * from issue_approvals'`), or watch
the tick log for `skipped_unapproved_issue`.

## VM configuration / migration steps

1. Deploy this change; `init_db` creates `issue_approvals` automatically on
   next start - no manual schema migration.
2. Set on the VM (if not already set): `GITHUB_REPOSITORY=tire0011/agent-lab`,
   `AGENT_GITHUB_WEBHOOK_SECRET`, and optionally `AGENT_MAILBOX_REPO=tire0011/agent-lab-mailbox`
   (webhook repository pin) and `NEXUS_TRUSTED_ACTORS` (extra logins allowed
   to push trusted mailbox content; default is the repo owner).
3. Keep `tire0011/agent-lab-mailbox` **private** with write access limited to
   trusted actors; keep the webhook secret and `AGENT_API_KEY` unchanged.
4. Migration of the existing backlog: after deploy, every open issue is
   pending. Approve the ones Nexus should keep working on by pushing one
   `issue_approval` message per issue (section above). Issues not approved
   simply stay pending - Nexus keeps running experiments that are already in
   flight and keeps adopting verified work; it only stops *starting* new
   issue work.
5. Only then make `agent-lab` public.

## Remaining risks

- The mailbox repository's own access control is the root of trust: anyone
  who can push to it (a collaborator) is trusted. The git-provenance check
  verifies *which account* pushed, and only accounts in the trusted set may
  affect execution - keep the collaborator list minimal.
- Owner-approved content is executed as-is: prompt injection inside an
  approved issue is possible by design (the approver is trusted); the
  goal-policy preflight (#408) is the second line of defense.
- Public read-only endpoints (`/health`, `/status`, `/diagnostics/public`,
  sanitized logs/mailbox listings) still expose status information to anyone;
  they are read-only and cannot trigger execution, but review their redaction
  before going public.
- The VM's intentionally unrestricted shell policy is unchanged; enforcement
  happens at the GitHub ingestion boundary, not in the shell.
