"""Shared setup for live tests against the real Nexus environment.

These tests talk to the real local llama-server and are run by hand, one at a
time, on the Nexus VM - usually through ``scripts/nexus-live-check.sh``. They
live outside ``tests/`` on purpose: pytest.ini only collects ``tests/``, so CI
and a plain ``pytest`` never pick them up.

How close to production:
- Inference goes through the production local-LLM queue (the
  ``local_llm_jobs`` table in Nexus's database, ``AGENT_DB_PATH``). A test's
  request waits for the single inference slot like every Nexus process does
  (#167), so it never talks to llama-server concurrently with Nexus and never
  runs on a queue of its own.
- Only what a test must not change is isolated: task sessions, memory and the
  loop guard live in a scratch database, so a test neither shows up in Nexus's
  memory index nor claims real sessions. Selected tools are never executed.

Conventions:
- A FAILED test is a reproduced regression; an ERROR (setup failure, e.g.
  llama-server down) means the environment could not answer the question.
- Every test's outcome plus whatever it put into ``live_result`` is printed as
  one JSON line at the end of the run, so mailbox outbox results can be
  compared across commits and models.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Keep controller logs out of the production log directory, like tests/ does.
os.environ.setdefault("AGENT_LOG_DIR", tempfile.mkdtemp(prefix="nexus-live-logs-"))

_RESULTS: list[dict[str, Any]] = []
_RECORDED: dict[str, dict[str, Any]] = {}


def _commit() -> str:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "unknown"


def _model_url() -> str:
    url = os.environ.get("LOCAL_MODEL_URL") or os.environ.get("LLAMA_CPP_BASE_URL")
    return (url or "http://127.0.0.1:8080/v1").rstrip("/")


@pytest.fixture(scope="session")
def llama_server() -> dict[str, str]:
    """The reachable local model server, or a setup ERROR if there is none."""
    url = _model_url()
    try:
        resp = httpx.get(f"{url}/models", timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"llama-server not reachable at {url}: {exc}", pytrace=False)
    models = resp.json().get("data") or [{}]
    return {"url": url, "model": str(models[0].get("id", "unknown"))}


def null_experiment_service() -> SimpleNamespace:
    """Resolves nothing, like a task with no experiment behind it.

    A service that answered every lookup would let an issue number in the task
    text masquerade as an experiment id.
    """

    def resolve(experiment_id: str) -> Any:
        raise KeyError(f"unknown experiment {experiment_id!r}")

    return SimpleNamespace(resolve_experiment=resolve)


def live_budget() -> Any:
    """Prompt budget from the host's memory right now, as production computes it."""
    from modules.prompt_budget import (
        compute_budget,
        read_mem_available_kb,
        read_swap_free_kb,
        read_swap_total_kb,
    )

    return compute_budget(
        mem_available_kb=read_mem_available_kb(),
        swap_free_kb=read_swap_free_kb(),
        swap_total_kb=read_swap_total_kb(),
    )


@pytest.fixture
def live_result(request) -> dict[str, Any]:
    """Free-form fields a test wants in its JSON result line."""
    data: dict[str, Any] = {}
    _RECORDED[request.node.nodeid] = data
    return data


@pytest.fixture(scope="session")
def nexus_llm_queue():
    """The production local-LLM job queue, shared with the running Nexus.

    Nexus serializes local inference in its database, not in a process: every
    Nexus process (API, tick, mailbox) queues there and runs a job only after
    claiming the one inference slot. Joining that same queue is what keeps a
    live test from racing Nexus for llama-server. A missing database is a
    setup ERROR - SQLite would otherwise quietly create an empty one, which is
    exactly the private queue this must never be.
    """
    from modules.local_llm_jobs import acquire_local_llm_job_service, release_local_llm_job_service
    from modules.run_store import DEFAULT_DB_PATH

    db_path = Path(DEFAULT_DB_PATH)
    if not db_path.is_file():
        pytest.fail(
            f"Nexus database {db_path} not found; set AGENT_DB_PATH to the database "
            "the running Nexus uses",
            pytrace=False,
        )
    service = acquire_local_llm_job_service(db_path=str(db_path))
    try:
        yield service
    finally:
        release_local_llm_job_service(service)


@pytest.fixture
def orchestrator(llama_server, nexus_llm_queue, tmp_path):
    """A real NexusTaskOrchestrator, one step at most.

    Inference runs on the production queue; sessions, memory and loop guard
    on a scratch database.
    """
    from modules.operations_registry import OperationsRegistry
    from modules.task_orchestrator import NexusTaskOrchestrator

    registry = OperationsRegistry(
        experiment_service=null_experiment_service(),
        db_path=str(tmp_path / "nexus.db"),
        repo_root=str(REPO_ROOT),
    )
    orch = NexusTaskOrchestrator(
        operations_registry=registry,
        experiment_service=null_experiment_service(),
        llm_job_service=nexus_llm_queue,
        max_steps=1,
    )
    try:
        yield orch
    finally:
        orch.close()


@pytest.fixture
def controller_turn(orchestrator, live_result):
    """Run one real controller turn and return the parsed decision.

    Renders the production prompt for ``task`` plus ``observations``, queues it
    on the production local-LLM queue and stops after parsing the chosen
    action. The selected tool is never executed. ``llm_seconds`` includes the
    time spent waiting for Nexus's own inference to finish.
    """
    from modules.task_orchestrator import _ORCHESTRATOR_RESPONSE_FORMAT

    def run(task: str, observations: list[dict[str, Any]], *, max_tokens: int = 700) -> dict:
        session = orchestrator.task_store.get_or_create(
            source_type="task", source_id=f"live-{time.time_ns()}", title=task
        )
        budget = live_budget()
        prompt, meta = orchestrator._build_prompt(session, task, [], observations, budget)
        started = time.monotonic()
        raw, provider = asyncio.run(
            orchestrator._schema_completion(
                prompt,
                session_id=session.id,
                max_tokens=max_tokens,
                response_format=_ORCHESTRATOR_RESPONSE_FORMAT,
                budget_plan=budget,
                role="controller",
            )
        )
        decision = orchestrator._parse(raw)
        live_result.update(
            provider=provider,
            prompt_chars=len(prompt),
            llm_seconds=round(time.monotonic() - started, 1),
            omitted_observation_indices=meta.get("omitted_observation_indices"),
            queue_db=orchestrator.llm_jobs.db_path,
            decision=decision,
        )
        return decision

    return run


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0][:300]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    # Record the call phase, or the setup phase when setup is what failed.
    if report.when == "call" or (report.when == "setup" and not report.passed):
        _RESULTS.append(
            {
                "test": item.nodeid,
                "outcome": "error" if report.when == "setup" else report.outcome,
                "seconds": round(report.duration, 1),
                "message": _first_line(call.excinfo.value) if call.excinfo else "",
            }
        )


def pytest_terminal_summary(terminalreporter):
    if not _RESULTS:
        return
    model = "unknown"
    try:
        model = str(httpx.get(f"{_model_url()}/models", timeout=5.0).json()["data"][0]["id"])
    except Exception:  # noqa: BLE001 - the summary must never fail the run
        pass
    common = {"commit": _commit(), "model": model}
    terminalreporter.write_sep("=", "live results (json)")
    for result in _RESULTS:
        line = {**common, **result, **_RECORDED.get(result["test"], {})}
        terminalreporter.write_line(json.dumps(line, sort_keys=True, default=str))
