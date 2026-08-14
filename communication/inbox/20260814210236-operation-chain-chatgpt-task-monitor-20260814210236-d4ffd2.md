---
type: operation_chain
created_by: rene-and-chatgpt
created_at: 2026-08-14T21:02:36.075Z
request_id: chatgpt-task-monitor-20260814210236-d4ffd2
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
      "id": "worktrees",
      "action": "git_worktrees",
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
    }
  ]
}
