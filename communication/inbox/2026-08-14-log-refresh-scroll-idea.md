---
type: idea
created_by: rene-and-chatgpt
---

Improve the Logs tab auto-refresh behavior so reading logs on desktop or iPhone is not interrupted every ten seconds.

Add an accessible Auto-refresh on/off control, preserve the user's scroll position and expanded log-entry state during refresh, and avoid replacing the rendered DOM when data is unchanged. When the user has scrolled away from the newest entries, do not jump the page; show a small "new log entries available" indicator that lets the user move to them explicitly. Manual refresh must remain available. Keep the behavior mobile-friendly and test success, failure, paused, unchanged-data, scroll-preservation, and new-entry cases.
