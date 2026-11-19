# Home GPU batch lane (#743)

Local-model work that can wait (bake-off, hunk replays, chat completions,
review and code drafts) can run on the owner's PC instead of the VM's slow
7B. Nexus writes a signed job into the mailbox repository, the PC answers it
through its loopback llama-server, and Nexus checks the answer with the same
checker the VM path uses. The PC never decides anything, and nothing in the
paid path waits on it.

The lane is opt-in with `NEXUS_HOME_GPU=1` on the Nexus processes that submit,
poll and score jobs.

## Trust model

| Boundary | Rule |
| --- | --- |
| What leaves the VM | Only text read from Git objects of the public `tire0011/agent-lab` repository at a commit that is reachable from the `main` tip GitHub reports (`git ls-remote https://github.com/tire0011/agent-lab`). The local `origin` URL and local refs are not trusted. Every string is refused if it holds a path outside the repository, a URL other than `https://github.com/tire0011/agent-lab/...` (including scheme-relative `//host` and bare `host/path` forms), or any credential shape of the shared scrubbers (`modules.redaction.contains_credential`, including bare GitHub tokens). At most 64 KiB per job. |
| Jobs to the PC | HMAC-SHA256 over job id, kind, deadline and input SHA-256, with a key only Nexus and the PC hold. `created_by: nexus` alone is not trusted. |
| Replies to Nexus | HMAC-SHA256 over every reply field and the answer, under a different domain label, so a job signature can never pass as a reply signature. Nexus reads replies only from the synced mailbox commit (`HEAD`), never from untracked or modified worktree files. |
| Code from a reply | Applied by the VM's SEARCH/REPLACE applier. The merged tests that score it run under `bwrap` with no network, a private `/tmp`, a read-only system and Python, and only the throwaway worktree writable. The Nexus database, the mailbox, the key and home directories are not mounted. Without a working `bwrap` the job waits and is not scored. |
| The PC handler | Sends one request to `http://127.0.0.1:<port>/v1/chat/completions`, with no proxy and no redirects. It runs no code from the payload and starts no process. The poll around it runs only `git`, with fixed arguments. |

## Set up the shared key (once)

The key is a random value of at least 32 bytes. It must not be readable by
other users (`chmod 600`), and it must never be committed.

1. On the VM, create the key file as the Nexus service user, for example with
   `python3 -c "import secrets; print(secrets.token_hex(32))" > /var/lib/nexus/home-gpu.key`
   and then `chmod 600 /var/lib/nexus/home-gpu.key`.
2. Set `NEXUS_HOME_GPU_KEY_FILE=/var/lib/nexus/home-gpu.key` and
   `NEXUS_HOME_GPU=1` for the Nexus mailbox poll, the tick and the API.
3. Copy the same value to the PC over a private channel, not through the
   mailbox, and store it in a file that only the owner can read.

## Exact owner steps on the PC

1. `git pull` the mailbox repository checkout on the PC. Also clone or pull
   the public agent-lab repository; the worker runs from there.
2. Start llama-server on loopback with the model you want to measure, for
   example Ornith-1.5-9B Q4_K_M with 24k context, on port 8080.
3. Run the worker from the agent-lab checkout:

   ```sh
   NEXUS_HOME_GPU_KEY_FILE=~/home-gpu.key \
     python -m handlers.home_gpu_worker --mailbox PATH/TO/mailbox --port 8080 --loop
   ```

   On Windows PowerShell, set `$env:NEXUS_HOME_GPU_KEY_FILE` first. The worker
   needs only the Python standard library and `git`. Each poll commits any
   reply that an interrupted earlier poll left, pulls the mailbox, answers
   every `inbox/*.md` with `to: home-gpu` and `type: batch_job`, then commits
   and pushes `outbox/<job_id>.reply.md`. Running a poll twice never calls the
   model twice for the same job.
4. If the mailbox client already has its own poll, register the handler
   there instead of running the loop:
   `handlers.home_gpu_worker.register(dispatch)` adds `batch_job`, which is
   called as `handle(meta, body, outbox_dir, key=key, port=port)`.
5. Check the result on the VM with `GET /home-gpu/jobs` or the
   `home_gpu_jobs_list` MCP tool. A job should go from `queued` to `answered`
   and then get `check_status: passed`.

## Submit jobs

Generic jobs (`chat_completion`, `review_draft`, `code_draft`) come from a
committed JSON request on public `main`:

```python
from modules.home_gpu.jobs import submit_job

job_id = submit_job("chat_completion", {"commit": commit_sha, "path": "benchmarks/request.json"})
```

A `chat_completion` request holds `messages` and optionally `grammar`,
`json_schema`, `seed` and `max_tokens`. The other kinds take a `prompt`. A
`code_draft` can also list `files`, the repository paths it may edit.
`submit_job` only stores the job. The next mailbox poll writes and publishes
it, so a crash between the two leaves a queued row that the next poll
publishes.

Hunk and bake-off jobs are created by the replay, which is also their checker:

```sh
python -m modules.local_shadow_replay_hunks EXPERIMENT_ID --via home-gpu
python scripts/local_model_bakeoff.py EXACT_LLAMA_MODEL_ID --via home-gpu
```

The first call returns while jobs are queued. The tick scores the answers
later. Re-run the bake-off command to refresh its aggregate row. Unit and
bake-off rows carry `host`, so home GPU numbers stay apart from VM numbers.

## Checkers

Inference and checking are recorded separately. `answered_at` and
`answer_host` record who produced the text, and `checked_at` and
`check_status` (`passed`, `failed`, `error`) record what the checker said.

| Kind | Checker |
| --- | --- |
| `hunk_replay`, `bakeoff_prompt` | Hunk replay: SEARCH/REPLACE applier, symbol splice, compile, merged tests (sandboxed for PC answers) |
| `chat_completion` | JSON-schema validation when the job has `json_schema`, otherwise a non-empty answer |
| `review_draft` | `nexus_reviewer.parse_review` |
| `code_draft` | `local_edit_format.edits_to_diff` against the public files at the job's commit, in a temporary directory |

## Deadlines, offline PC and fallback

- Deadlines are enforced locally on every mailbox poll and tick, also while
  the mailbox cannot be synced. A job within five minutes of its deadline is
  not published any more.
- The default deadline is six hours. If no valid reply has arrived in the
  last 24 hours (or since the first publication), published jobs fall back
  at once. New jobs are still published, so a PC that comes back is noticed.
  A valid PC answer before the deadline still wins over a fallback that the
  VM has not started yet.
- Batch kinds (`chat_completion`, `hunk_replay`, `bakeoff_prompt`) go to the
  VM between 00:00 and 06:00 UTC, only when the local server is idle.
  Generic jobs go through the shared local job service, with the local time
  budget as admission (`home_gpu_fallback_unaffordable` when the prompt does
  not fit). Timeouts, connection, protocol and stuck errors are retried with
  back-off up to three times. Other errors end the job with
  `home_gpu_fallback_failed`.
- Interactive kinds (`review_draft`, `code_draft`) are dropped with
  `home_gpu_expired`.
- One hunk job never blocks the others. A job whose replay already finished
  is closed with that result. A job that cannot make progress (for example
  because of a worktree failure, a busy server or a missing sandbox) gets a
  growing back-off, and the next job is tried. After eight attempts the job
  ends with `check_status: error`.
