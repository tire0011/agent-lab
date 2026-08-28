# Nexus runtime paths

This file records the important filesystem paths used by the Nexus Azure VM. Keep it updated when deployment layout changes.

## API service configuration

`agent-api.service` loads its runtime environment from:

```text
/etc/agent-lab/agent.env
```

`systemctl show agent-api.service -p Environment` does not necessarily show variables loaded through `EnvironmentFile=`. To verify the configured environment file, use:

```bash
systemctl show agent-api.service -p EnvironmentFiles
systemctl cat agent-api.service
```

## Repositories

Code repository:

```text
/agent
```

Separate Git-backed mailbox repository:

```text
/var/lib/nexus/mailbox-repo
```

The corresponding runtime setting in `/etc/agent-lab/agent.env` is:

```text
AGENT_MAILBOX_REPO_PATH=/var/lib/nexus/mailbox-repo
```

The mailbox repository is intentionally separate from `/agent`. Inbox/outbox Git synchronization and mailbox clean-worktree checks must operate on `/var/lib/nexus/mailbox-repo`, not on the code repository.

Useful check:

```bash
git -C /var/lib/nexus/mailbox-repo status --short
```

## Runtime state

SQLite database:

```text
/var/lib/nexus/agent.db
```

Mailbox lock:

```text
/var/lib/nexus/mailbox.lock
```

## Important fallback behavior

Mailbox path resolution currently falls back to `/agent` when `AGENT_MAILBOX_REPO_PATH` is missing. On the production VM this is not the intended layout. A missing mailbox path can therefore make unrelated local changes in `/agent` cause mailbox polling to report `dirty_worktree`.

When diagnosing `dirty_worktree`, first verify:

```bash
systemctl show agent-api.service -p EnvironmentFiles
sudo grep '^AGENT_MAILBOX_REPO_PATH=' /etc/agent-lab/agent.env
git -C /var/lib/nexus/mailbox-repo status --short
```
