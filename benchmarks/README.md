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

## Test-first on and off (#636)

    python scripts/run_benchmark.py run --test-first on    # prints RUN_ON
    python scripts/run_benchmark.py run --test-first off   # prints RUN_OFF
    python scripts/run_benchmark.py test-first-report RUN_ON RUN_OFF

With `--test-first` every task goes to the local model first (as a `small`
experiment), with or without its generated failing tests (`LocalCodingWorker`
`test_first`); without the flag the benchmark runs as before. When the local
attempt falls back to a paid coder, the generated test file is taken out of the
worktree and handed to the paid coder and the reviewer under
`MODEL-WRITTEN TESTS (may be rejected)`; it is never required in the final diff.

| Column | Source |
|---|---|
| `test_first` | 1 on, 0 off, `NULL` for a run without `--test-first` |
| `tests_generated`, `generated_tests_failing_on_start` | Generated tests the local worker kept; each failed on the base tree |
| `generated_tests_passing_on_merge` | Of those, how many pass on the final tree of a succeeded experiment (the original generated file, run under a fresh module name); `NULL` without a merge candidate |
| `local_only_success` | 1 when the experiment succeeded on the local model with no paid call |
| `paid_calls` | Coding-agent calls that were not the local model, repairs included |
