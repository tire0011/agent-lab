---
type: operation_chain
created_by: rene-and-chatgpt
created_at: 2026-08-14T20:48:29.680Z
request_id: chatgpt-nexus-status-v2-20260814204829-48ead3
---

{
  "stop_on_error": false,
  "steps": [
    {
      "id": "git-status",
      "action": "git_status",
      "arguments": {}
    },
    {
      "id": "git-log",
      "action": "git_log",
      "arguments": {
        "limit": 10
      }
    },
    {
      "id": "worktrees",
      "action": "git_worktrees",
      "arguments": {}
    },
    {
      "id": "api-service",
      "action": "service_status",
      "arguments": {}
    },
    {
      "id": "disk",
      "action": "disk",
      "arguments": {}
    },
    {
      "id": "memory",
      "action": "memory",
      "arguments": {}
    },
    {
      "id": "problems",
      "action": "problems_list",
      "arguments": {}
    },
    {
      "id": "inbox",
      "action": "inbox_status",
      "arguments": {}
    },
    {
      "id": "outbox",
      "action": "outbox_status",
      "arguments": {}
    }
  ]
}
