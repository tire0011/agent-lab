# Experiment benchmark (#343)

Shows whether a change of model, provider, coding CLI or prompt makes
`new_experiment` better or worse. `scripts/model-benchmark.sh` answers a
different question: which local model suits the controller.

## Tasks

`tasks/*.md` are real issues that have been merged. Each one pins the commit
the issue was started from and names the non-test files the merged fix touched.
Front matter:

| Key | Meaning |
|---|---|
| `base_commit` | Full hash the experiment worktree starts from |
| `expected_files` | Comma-separated files the merged fix touched |
| `test_modules` | Comma-separated test modules that decide "tests green" |
| `tests_green_points` | Points for green tests on a non-empty change |
| `expected_files_points` | Points, shared evenly across `expected_files` |
| `evaluation_points` | Points for a `pass` from `modules/experiment_evaluation.py` |

The points add up to 100 and the body is the goal. The set is frozen: editing a
file makes older runs not comparable, and the report says so instead of giving
a verdict. Add a new file rather than changing one. `test_modules` must be green
on `base_commit`; a task's tests are a fixed set, not the agent's new tests.

## Running

    python scripts/run_benchmark.py run --provider claude --model <name>
    python scripts/run_benchmark.py baseline <run_id>
    python scripts/run_benchmark.py report <run_id>

Without `--provider` the run uses the normal rotation between the coding agents,
which is the current production setup and the first baseline. `--prompt-file`
appends a file to every coding-agent call; `--task` runs only the named tasks,
though the report then calls the result not comparable with a full run. A full
run makes real, billed agent calls for all four tasks.

## Results

Runs go to `benchmark.db` next to the production database, never into it:
adoption, the planner and `problems` would otherwise treat them as real work.
Each task's worktree and branch are removed after its row is written.

| Column | Source |
|---|---|
| `score` | The rubric above, 0 to 100 |
| `input_tokens`, `output_tokens` | Every coding-agent call, repairs included |
| `runtime_ms` | Wall clock of the whole experiment, tests included |
| `estimated_cost` | Tokens and the production `model_prices` row for this `provider` and `model` |
| `peak_memory_bytes` | Highest resident memory of the runner and its child processes, sampled every second |

Unknown values stay `NULL` and the report shows `n/a`, never 0. Cost is unknown
without `--model` or without a matching `model_prices` row. Tokens are what each
CLI reports as `input_tokens`/`output_tokens`; the two count cached input
differently, so compare tokens across providers with care.
