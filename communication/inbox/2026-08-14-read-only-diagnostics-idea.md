---
type: idea
created_by: rene-and-chatgpt
---

Add a safe read-only diagnostic request type to the Git-backed mailbox so humans and ChatGPT can inspect Nexus without SSH.

Use predefined allowlisted actions rather than arbitrary shell commands. Initial actions could include git_status, git_diff_stat, git_recent_commits, git_worktrees, experiment_status, experiment_log_tail, service_status, disk_usage, and test_summary.

Execute fixed argument lists with shell disabled, a fixed repository root, validated experiment IDs, short timeouts, bounded output, central redaction, structured logging, and automatic sanitized results in communication/outbox. Never accept free-form commands, paths, environment variables, or unrestricted options from mailbox messages.
