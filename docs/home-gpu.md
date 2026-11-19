# Home GPU batch lane

The lane is opt-in with `NEXUS_HOME_GPU=1` on the Nexus processes that submit,
poll, and score jobs. It shares the existing mailbox configuration and SQLite
database. Nexus publishes through the regular mailbox poll and checks replay
answers during the initiative tick’s local replay phase.

## Submit and inspect jobs

Use a full commit SHA reachable from the canonical `origin/main` and a
repository-relative path to a committed JSON request. For example, a committed
`chat_completion` request can contain
`{"messages":[{"role":"user","content":"Summarize this public example."}],"seed":42,"max_tokens":128}`.
Optional constraints are `grammar` and `json_schema`. The other kinds take a
`prompt` instead of messages. A `hunk_replay` can also include
`expected_diff_refs`.

```python
from modules.home_gpu.jobs import submit_job

job_id = submit_job("chat_completion", {"commit": commit_sha, "path": "benchmarks/request.json"})
```

The public remote-tracking ref is the trust anchor. Inputs are loaded from Git
objects and checked for outside paths, foreign URLs, credential patterns, and
the 64 KiB wire payload limit. Hunk requests are assembled from the merged
commit subject and reconstructed source at the public base and merged commits.

Use the `home_gpu_jobs_list` MCP tool or the authenticated
`GET /home-gpu/jobs` endpoint to see status and timing. A Nexus-side caller can
retrieve the stored answer with `modules.home_gpu.collect.get_job` and apply
its own checker. Job records include server inference time and total elapsed
time. The default deadline is six hours. After a day without a valid reply
since first publication or last contact, new jobs enter fallback or expire.
A valid reply before its deadline restores contact, including a reply to a
job already assigned to the VM. VM fallback runs from 00:00 to 06:00 UTC when
the local server is idle. Review and code drafts expire with
`home_gpu_expired`.

## Run the existing benchmarks

```sh
python -m modules.local_shadow_replay_hunks EXPERIMENT_ID --via home-gpu
python scripts/local_model_bakeoff.py EXACT_LLAMA_MODEL_ID --via home-gpu
```

The initial call can return while jobs are queued. The initiative tick
consumes ready hunk results using the existing SEARCH/REPLACE applier,
symbol splice, compiler, and merged tests. Re-run the same bake-off command to
refresh its aggregate row after the unit scores arrive. The bake-off seed and
fixture select a stable run ID. The home lane uses the PC’s already loaded
model and checks that the reply reports that model ID. Hunk results and
bake-off rows carry `host`. VM fallbacks are scored and aggregated under
`host=vm`.

## Set up the mailbox client on the PC

1. Pull the PC’s mailbox checkout.
2. Copy [the portable handler](../handlers/batch_job.py) into
   `mailbox_client/handlers/batch_job.py` in the mailbox repository. Register
   `batch_job` in that client’s dispatch table so a routed message calls
   `handle(meta, body, outbox_path, port=llama_port)`. Pass the parsed front
   matter, JSON body, and mailbox outbox directory. The client retains
   responsibility for polling, routing, archiving, and syncing Git.
3. Run llama-server on loopback and set the handler port to match it. The
   default is `8080`. Start the mailbox client with recipient ID `home-gpu`.
4. Submit a public smoke request and check that the job becomes `answered`.
   The worker returns a reply for the same job ID and input SHA-256 and
   records the llama-server model, token counts, elapsed seconds, and
   generation rate. The worker’s only external call is the loopback
   completion request.

The mailbox checkout and the PC are separate from the agent-lab workspace.
Verify dispatch registration and the smoke request there before enabling the
lane for regular runs.
