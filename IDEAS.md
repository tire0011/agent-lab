# IDEAS.md

Ideas for agent-lab now live as GitHub issues (#340). The former
`## Ideas` list was reviewed against the code and the open issues in
`docs/ideas-to-issues.md`, which proposes the issues to create; its full
text stays there and in git history.

For a record of what has actually been *built and verified*, see
`ARCHITECTURE.md` ("Current architecture (implemented)") and
`DECISIONS.md` (accepted/deferred decisions).

## New ideas

Propose a new idea as a GitHub issue. Ideas submitted via `POST /ideas`
or a mailbox `idea` message are still appended to a
`## Ideas` / `### Inbox` section in this file, one line each; turn them
into issues and remove them from here.

## States

Each idea may carry a lightweight state, written as a trailing
`` `state` `` marker. A state is optional — an idea with no marker is
implicitly `new`.

- `new` — captured, not yet looked at.
- `exploring` — actively being investigated or discussed, no commitment yet.
- `selected` — chosen as worth a concrete attempt, not yet built/tested.
- `tested` — a version exists and has been run/measured somehow.
- `adopted` — kept as part of the system based on evidence.
- `rejected` — tried or considered and deliberately not pursued (a reason
  is helpful but not required).

Ideas are not required to have a priority. Ordering within a section is
not a significance ranking.

## Open unknowns

Unknowns worth tracking without necessarily turning each into an issue:

- Which evaluation strategy is robust enough for automated gating.
- Which low-cost model mix offers acceptable quality.
- How much autonomy is safe on current infrastructure limits.
- Which VM size gives the best cost/performance for coding CLI workloads.
- Whether an ephemeral second VM is cheaper and safer than testing on the main VM.
- Which memory retrieval strategy remains simple enough while still scaling with accumulated experience.
- Which stable Codex CLI output or event format exposes usage and session metadata reliably.
