from pathlib import Path

path = Path("modules/mailbox_poll_runner.py")
text = path.read_text(encoding="utf-8")
old = '''    run_name = threading.current_thread().name\n    if not _worktree_safe_for_mailbox_sync(\n        mailbox_module, repo_path, ignored_untracked_prefixes\n    ):\n        logger.warning("Mailbox sync refused run=%s reason=dirty_worktree", run_name)\n        return False\n'''
new = '''    run_name = threading.current_thread().name\n    # Preserve the original direct helper contract when no machine-managed\n    # untracked paths need to be tolerated.  The mailbox poller passes the\n    # configured outbox prefix explicitly; legacy/direct callers continue to\n    # use git_is_clean(), which also keeps their retry behavior unchanged.\n    if ignored_untracked_prefixes:\n        worktree_safe = _worktree_safe_for_mailbox_sync(\n            mailbox_module, repo_path, ignored_untracked_prefixes\n        )\n    else:\n        worktree_safe = mailbox_module.git_is_clean(repo_path)\n    if not worktree_safe:\n        logger.warning("Mailbox sync refused run=%s reason=dirty_worktree", run_name)\n        return False\n'''
if text.count(old) != 1:
    raise SystemExit(f"expected one sync precheck block, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("mailbox sync compatibility patch applied")
