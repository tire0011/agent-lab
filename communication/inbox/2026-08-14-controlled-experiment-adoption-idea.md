---
type: idea
created_by: rene-and-chatgpt
---

Add a controlled experiment-adoption workflow so successful Nexus experiments can be integrated without manually repeating Git commands.

Initially require explicit human approval through the Web UI or authenticated API. A succeeded experiment should become ready_for_adoption and expose a sanitized diff summary, changed files, test result, runtime, token usage, and risks.

On adoption, use a repository-wide Git lock and verify that the experiment succeeded, tests passed, its worktree and branch still match the recorded experiment, the main working tree is clean, and the remote state is known. Create a deterministic commit on the experiment branch, integrate it into a temporary validation worktree based on current main, rerun the full test suite with the isolated verification environment, and only update main when integration tests pass without conflicts. Never resolve conflicts automatically. On conflict, failed tests, concurrent repository changes, or push failure, leave main unchanged and move the experiment to needs_review with complete structured logs and a recoverable retry path.

Keep adoption idempotent and auditable. Add preview, adopt, status, and retry capabilities plus a mobile-friendly Adopt button. Do not enable fully automatic adoption until several manually approved adoptions have been measured and shown safe.
