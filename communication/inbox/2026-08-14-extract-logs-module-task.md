---
type: task
created_by: rene-and-chatgpt
---

Perform one behavior-preserving frontend refactoring to reduce the context needed for future Logs-tab work.

Current baseline:
- static/app.js is about 901 lines.
- The previous small Logs-tab feature experiment used 1,102,844 total tokens.
- We want to test whether a smaller focused module reduces token use in a later comparable task.

Scope:
- Extract only the existing Logs-tab functionality from static/app.js into a dedicated static/logs.js module.
- Do not refactor other tabs or backend code.
- Preserve all current behavior exactly: authenticated experiment logs, public fallbacks, filters, experiment selection, manual refresh, 10-second auto-refresh, visible-entry count, last-successful-refresh timestamp, error behavior, expandable details, safe textContent rendering, and mobile layout.
- Keep shared API-key and request behavior centralized rather than duplicated.
- Use a small explicit integration boundary between app.js and logs.js.
- Load scripts in a deterministic order without adding a build step, framework, package, or external dependency.
- Avoid new global variables except one clearly named namespace/module boundary if required by the existing no-build architecture.
- Do not change API contracts, visible wording, styling, refresh behavior, or security behavior.
- Do not touch existing experiment worktrees.

Verification:
- Add or adjust focused tests proving logs.js is served and the Logs tab retains its existing contract.
- Run the focused frontend/API tests.
- Run the complete test suite.
- Report app.js and logs.js line counts before/after, changed files, dependency boundary, focused/full test results, runtime, and any limitations.
- Do not commit or push.
