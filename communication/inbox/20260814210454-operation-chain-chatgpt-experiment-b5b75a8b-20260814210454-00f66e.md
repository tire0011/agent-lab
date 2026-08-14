---
type: operation_chain
created_by: rene-and-chatgpt
created_at: 2026-08-14T21:04:54.820Z
request_id: chatgpt-experiment-b5b75a8b-20260814210454-00f66e
---

{
  "stop_on_error": false,
  "steps": [
    {
      "id": "experiment",
      "action": "experiment_show",
      "arguments": {
        "experiment_id": "b5b75a8b"
      }
    },
    {
      "id": "logs",
      "action": "experiment_logs",
      "arguments": {
        "experiment_id": "b5b75a8b",
        "limit": 120
      }
    }
  ]
}
