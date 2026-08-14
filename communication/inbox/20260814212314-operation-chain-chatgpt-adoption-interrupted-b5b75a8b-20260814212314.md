---
type: operation_chain
created_by: rene-and-chatgpt
created_at: 2026-08-14T21:23:14.610Z
request_id: chatgpt-adoption-interrupted-b5b75a8b-20260814212314
---

{
  "stop_on_error": false,
  "steps": [
    {"id":"git-status","action":"git_status","arguments":{}},
    {"id":"git-log","action":"git_log","arguments":{"limit":20}},
    {"id":"experiment","action":"experiment_show","arguments":{"experiment_id":"b5b75a8b"}},
    {"id":"experiment-logs","action":"experiment_logs","arguments":{"experiment_id":"b5b75a8b","limit":250}},
    {"id":"api-logs","action":"service_logs","arguments":{"limit":300}},
    {"id":"problems","action":"problems_list","arguments":{}},
    {"id":"outbox","action":"outbox_status","arguments":{}}
  ]
}
