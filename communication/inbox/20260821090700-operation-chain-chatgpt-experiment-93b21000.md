---
type: operation_chain
created_by: rene-and-chatgpt
request_id: chatgpt-experiment-93b21000-20260821090700
---

{
  "steps": [
    {
      "id": "experiment",
      "action": "experiment_show",
      "arguments": {
        "experiment_id": "93b21000cb8c4cc09cdd783d104244d2"
      }
    },
    {
      "id": "logs",
      "action": "experiment_logs",
      "arguments": {
        "experiment_id": "93b21000cb8c4cc09cdd783d104244d2",
        "limit": 100
      }
    },
    {
      "id": "service",
      "action": "service_status",
      "arguments": {}
    },
    {
      "id": "mailbox",
      "action": "mailbox_status",
      "arguments": {}
    }
  ]
}
