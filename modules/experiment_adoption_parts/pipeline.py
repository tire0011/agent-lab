"""The adoption pipeline of :class:`ExperimentAdoptionService`."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid

from pathlib import Path

from modules.experiment_evaluation import (
    evaluate_implementation,
    parse_git_status_short,
)
from modules.experiment_service import (
    _DEFAULT_TEST_COMMAND_KEY,
    _TEST_COMMANDS,
    _run_git,
    _utcnow_iso,
)
from modules.github_work_tracking import (
    GitHubWorkTrackingError,
    MergeForbiddenError,
)
from modules.mailbox import MailboxLock
from modules.redaction import (
    bounded_lines,
    sanitized_reason,
)
from modules.test_run_log import global_heartbeat_bridge
from modules.verification_runner import run_verification_test_monitored

from modules.experiment_adoption_parts.core import (
    ExperimentAdoption,
    ExperimentAdoptionError,
    STATUS_ADOPTED,
    STATUS_ADOPTING,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_READY_FOR_REVIEW,
    _DEFAULT_GIT_TIMEOUT_SECONDS,
    _DEFAULT_TARGET_BRANCH,
    _collapse_summary,
    _safe_list,
)


class PipelineMixin:
    """The adoption pipeline of :class:`ExperimentAdoptionService`."""

    def _run_adoption_pipeline(
        self, adoption_id: str, experiment_id: str, *, resume: bool = False
    ) -> ExperimentAdoption:
        elog = self.experiment_service.get_experiment_logger(experiment_id)
        lock = MailboxLock(self.lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not lock.acquire():
            self._set_status(
                experiment_id,
                STATUS_FAILED,
                ended_at=_utcnow_iso(),
                reason=sanitized_reason("Eine andere Übernahme läuft bereits."),
                updated_at=_utcnow_iso(),
            )
            return self.get(experiment_id)

        integration_path: Path | None = None
        keep_integration = False
        try:
            if not self._transition_status(
                experiment_id,
                STATUS_QUEUED,
                STATUS_ADOPTING,
                started_at=_utcnow_iso(),
                updated_at=_utcnow_iso(),
            ):
                return self.get(experiment_id)

            elog.info("adoption", "adoption_started", "Starting persisted experiment adoption")
            experiment_for_tracking = self.experiment_service.get_experiment(experiment_id)
            try:
                self.github_work_tracking.recover_experiment(experiment_for_tracking)
            except GitHubWorkTrackingError as exc:
                return self._finish_review(
                    experiment_id,
                    reason=f"GitHub lifecycle blocked before adoption: {exc}",
                    review_blocker=self._lifecycle_readiness_blocker(),
                    elog=elog,
                )
            fetch_proc = _run_git(
                ["fetch", "origin", _DEFAULT_TARGET_BRANCH],
                cwd=str(self.repo_root),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if fetch_proc.returncode != 0:
                return self._finish_failure(
                    experiment_id,
                    reason="git fetch origin main failed",
                    elog=elog,
                )

            resumed_commit_sha = self._resumable_commit_sha(experiment_id) if resume else None
            if resumed_commit_sha is not None:
                experiment = self.experiment_service.get_experiment(experiment_id)
                worktree_path = str(experiment.worktree_path)
                record = self.get(experiment_id)
                actual_files = _safe_list(record.actual_files or [])
                expected_files = _safe_list(record.expected_files or [])
                experiment_commit_sha = resumed_commit_sha
                commit_message = self._commit_message(
                    experiment_id, experiment.goal, experiment.plan
                )
                elog.info(
                    "adoption",
                    "adoption_resumed_from_commit",
                    "Resuming adoption from the experiment commit created before the restart",
                    experiment_commit_sha=experiment_commit_sha,
                )
            else:
                validation = self._validate_startable(experiment_id, persist_review=False)
                experiment = validation["experiment"]
                worktree_path = str(experiment.worktree_path)
                snapshot = validation["snapshot"]
                actual_files = validation["actual_files"]
                expected_files = validation["expected_files"]
                elog.debug(
                    "adoption",
                    "adoption_diff_captured",
                    "Captured experiment diff prior to commit",
                    changed_files=actual_files,
                    diff_stat=sanitized_reason(snapshot["diff_stat"], max_chars=1000),
                )
                # Validation proved there is something to adopt and in which shape:
                # an uncommitted diff to commit, or a commit an earlier attempt left.
                experiment_commit_sha = validation["committed_commit_sha"]
                commit_message = self._commit_message(
                    experiment_id, experiment.goal, experiment.plan
                )

            if experiment_commit_sha is None:
                _run_git(["reset", "HEAD", "--"], cwd=worktree_path, timeout=60, elog=elog, phase="adoption")
                for rel_path in actual_files:
                    add_proc = _run_git(
                        ["add", "--", rel_path],
                        cwd=worktree_path,
                        timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                        elog=elog,
                        phase="adoption",
                    )
                    if add_proc.returncode != 0:
                        return self._finish_failure(
                            experiment_id,
                            reason=f"git add failed for {Path(rel_path).name}",
                            actual_files=actual_files,
                            expected_files=expected_files,
                            elog=elog,
                        )

                commit_proc = _run_git(
                    ["commit", "--no-verify", "-m", commit_message],
                    cwd=worktree_path,
                    timeout=120,
                    elog=elog,
                    phase="adoption",
                )
                if commit_proc.returncode != 0:
                    return self._finish_failure(
                        experiment_id,
                        reason="Der Experiment-Commit konnte nicht erstellt werden.",
                        actual_files=actual_files,
                        expected_files=expected_files,
                        elog=elog,
                    )
                experiment_commit_sha = self._git_stdout(["rev-parse", "HEAD"], worktree_path, elog).strip() or None

            # Persist the source commit whichever way it was obtained; a requeue
            # resets this column, so an already-committed change must write it back
            # or the next attempt is blind again the same way.
            self._set_status(
                experiment_id,
                STATUS_ADOPTING,
                experiment_commit_sha=experiment_commit_sha,
                actual_files_json=json.dumps(_safe_list(actual_files)),
                expected_files_json=json.dumps(_safe_list(expected_files)),
                updated_at=_utcnow_iso(),
            )

            integration_path = self.worktrees_root / f"adopt-{experiment_id[:8]}-{uuid.uuid4().hex[:8]}"
            integration_path.parent.mkdir(parents=True, exist_ok=True)
            add_worktree = _run_git(
                ["worktree", "add", str(integration_path), f"origin/{_DEFAULT_TARGET_BRANCH}"],
                cwd=str(self.repo_root),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if add_worktree.returncode != 0:
                return self._finish_failure(
                    experiment_id,
                    reason="Der Integrations-Worktree konnte nicht erstellt werden.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    elog=elog,
                )

            # The whole change since the experiment branch left main, not only its
            # last commit: a retry builds on earlier attempts, and picking just its
            # own commit either conflicts or lands half a change (#455, reverted #410).
            squash = _run_git(
                ["merge", "--squash", experiment_commit_sha or "HEAD"],
                cwd=str(integration_path),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if squash.returncode != 0:
                keep_integration = True
                conflicted = self._git_stdout(
                    ["diff", "--name-only", "--diff-filter=U"], str(integration_path), elog
                ).split()
                _run_git(["reset", "--hard", "HEAD"], cwd=str(integration_path), timeout=60, elog=elog, phase="adoption")
                return self._finish_review(
                    experiment_id,
                    reason="Integrationskonflikt: " + (", ".join(conflicted[:10]) or "merge failed"),
                    actual_files=actual_files,
                    expected_files=expected_files,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )
            # Earlier attempts' files were never checked by an adoption.
            staged = self._git_stdout(["diff", "--cached", "--name-only"], str(integration_path), elog).split()
            for rel_path in staged:
                denied = self._denied_path_reason(rel_path)
                if denied is not None:
                    keep_integration = True
                    return self._finish_review(
                        experiment_id,
                        reason=denied,
                        actual_files=actual_files,
                        expected_files=expected_files,
                        diagnostic_worktree_path=str(integration_path),
                        elog=elog,
                    )

            evidence = self._exact_commit_ci(experiment_id, phase="adoption", elog=elog)
            ci = evidence.verification
            checkpoint_sha = evidence.checkpoint_sha
            exact_ci = evidence.exact
            verification_summary: str
            if evidence.passed:
                test_exit_code = 0
                test_output = (
                    "Reused authoritative GitHub Actions verification for exact checkpoint "
                    f"{checkpoint_sha}; workflow=tests.yml; run_id={ci.run_id}; conclusion={ci.conclusion}."
                )
                verification_summary = (
                    f"authoritative GitHub Actions passed for exact checkpoint `{checkpoint_sha}`"
                )
                elog.info(
                    "adoption",
                    "authoritative_ci_reused",
                    "Reusing successful exact-commit GitHub Actions verification; local full-suite fallback skipped",
                    commit_sha=checkpoint_sha,
                    run_id=ci.run_id,
                    conclusion=ci.conclusion,
                    url=ci.url,
                )
            elif ci.status == "failed" and exact_ci:
                keep_integration = True
                return self._finish_failure(
                    experiment_id,
                    reason=(
                        "Die autoritative GitHub-CI für den verifizierten Commit ist fehlgeschlagen; "
                        "ein lokaler Fallback darf dieses Ergebnis nicht überschreiben."
                    ),
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=1,
                    test_output=(
                        f"GitHub Actions exact checkpoint {checkpoint_sha}: "
                        f"conclusion={ci.conclusion}; run_id={ci.run_id}; reason={ci.reason or 'none'}"
                    ),
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )
            else:
                if ci.status == "passed" and not exact_ci:
                    elog.warning(
                        "adoption",
                        "authoritative_ci_sha_mismatch",
                        "Ignoring GitHub CI result because it does not match the persisted checkpoint SHA",
                        checkpoint_sha=checkpoint_sha,
                        ci_commit_sha=ci.commit_sha,
                    )
                else:
                    elog.info(
                        "adoption",
                        "local_tests_fallback_started",
                        "Authoritative exact-commit GitHub CI is unavailable; running local fallback tests",
                        checkpoint_sha=checkpoint_sha,
                        ci_status=ci.status,
                        reason=ci.reason,
                    )
                test_command_template = _TEST_COMMANDS[_DEFAULT_TEST_COMMAND_KEY]
                test_command = [part.format(python=self.experiment_service.test_python_executable) for part in test_command_template]
                watchdog = self.experiment_service._get_test_watchdog_config()
                expected_total = self.experiment_service._get_expected_test_total()
                test_run = run_verification_test_monitored(
                    test_command,
                    cwd=str(integration_path),
                    run_id=f"adoption-{experiment_id}",
                    config=watchdog,
                    expected_total=expected_total,
                    # Same heartbeat propagation as experiment verification:
                    # one progress entry per watchdog tick in the global log,
                    # every per-test entry in the dedicated test log.
                    on_heartbeat=global_heartbeat_bridge(elog, "adoption"),
                )
                self.experiment_service._remember_test_total(test_run.tests_total)
                test_exit_code = test_run.exit_code
                test_output = test_run.output
                verification_summary = f"local fallback integration tests passed (exit code {test_exit_code})"
                if test_run.timed_out:
                    keep_integration = True
                    elog.log_process(
                        "adoption",
                        "integration_tests_stalled" if test_run.stalled else "integration_tests_safety_cap_exceeded",
                        test_run.stall_diagnostics or "Integration test run aborted by the progress watchdog",
                        command=test_command, cwd=str(integration_path),
                        duration_ms=test_run.duration_ms,
                        timed_out=True, level="ERROR",
                        stall_timeout_seconds=watchdog.stall_timeout_seconds,
                        safety_cap_seconds=watchdog.safety_cap_seconds,
                        test_log_path=test_run.test_log_path,
                        **(test_run.progress or {}),
                    )
                elif test_run.process_died:
                    keep_integration = True
                    elog.log_process(
                        "adoption", "integration_tests_process_died",
                        test_run.stall_diagnostics or "Integration test process was killed before it could finish",
                        command=test_command, cwd=str(integration_path),
                        duration_ms=test_run.duration_ms, level="ERROR",
                        test_log_path=test_run.test_log_path,
                        **(test_run.progress or {}),
                    )
                elif test_run.exception is not None:
                    keep_integration = True
                    elog.error(
                        "adoption", "integration_tests_exception",
                        "Integration test command raised an exception",
                        exc=test_run.exception, command=test_command,
                        cwd=str(integration_path),
                    )
                else:
                    elog.log_process(
                        "adoption", "integration_tests_completed",
                        f"Integration tests finished with exit code {test_exit_code}",
                        command=test_command, cwd=str(integration_path),
                        duration_ms=test_run.duration_ms,
                        exit_code=test_exit_code,
                        stdout=test_run.stdout, stderr=test_run.stderr,
                    )

            bounded_test_output = "\n".join(bounded_lines(test_output, max_lines=200))
            if test_exit_code != 0:
                keep_integration = True
                return self._finish_failure(
                    experiment_id,
                    reason=(
                        "Die Integrationstests sind fehlgeschlagen."
                        if test_exit_code is not None
                        else "Die Integrationstests konnten nicht erfolgreich abgeschlossen werden."
                    ),
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            integration_status = _run_git(
                ["status", "--short", "--untracked-files=all"],
                cwd=str(integration_path),
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase="adoption",
            )
            integrated_changed = parse_git_status_short(integration_status.stdout)
            eval_result = evaluate_implementation(
                experiment.plan,
                experiment.goal,
                integrated_changed,
                str(integration_path),
            )
            # Soft gate findings (#519) go to the reviewer; without one they stay a
            # hard stop here, before anything is pushed.
            integrated_soft = eval_result.soft_findings() if self.reviewer is not None else None
            review_findings = [
                f for f in (
                    (experiment.quality_evaluation or {}).get("review_findings"),
                    integrated_soft,
                ) if f
            ]
            if eval_result.outcome != "pass" and integrated_soft is None:
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason=f"Qualitäts-Gate auf integriertem Stand: {eval_result.reason}",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            integrated_commit_proc = _run_git(
                ["commit", "--no-verify", "-m", commit_message],
                cwd=str(integration_path),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if integrated_commit_proc.returncode != 0:
                keep_integration = True
                return self._finish_failure(
                    experiment_id,
                    reason="Der Integrations-Commit konnte nicht erstellt werden.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )
            integrated_commit_sha = self._git_stdout(["rev-parse", "HEAD"], str(integration_path), elog).strip() or None

            published_sha = integrated_commit_sha
            work_ref = self.github_work_tracking.for_experiment(experiment_id)
            if work_ref is None:
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason="Für das Experiment fehlt die persistierte GitHub-Issue-Zuordnung.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )
            try:
                self.github_work_tracking.require_lifecycle_ready()
            except GitHubWorkTrackingError as exc:
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason=f"GitHub lifecycle ist für die Veröffentlichung nicht bereit: {exc}",
                    review_blocker=self._lifecycle_readiness_blocker(),
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            try:
                published_sha = self.github_work_tracking.publish_final_tree(
                    work_ref,
                    integrated_commit_sha=integrated_commit_sha,
                    worktree_path=str(integration_path),
                    experiment_id=experiment_id,
                )
            except GitHubWorkTrackingError as exc:
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason=f"Der finale Push auf den Issue-Branch ist fehlgeschlagen: {exc}",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            # CI ran on the experiment branch; what merges is that change on top of
            # today's main. Test exactly that tree first (#455).
            if published_sha and not (exact_ci and evidence.passed and published_sha == checkpoint_sha):
                final_ci = self.github_work_tracking.wait_for_commit_ci(
                    published_sha, experiment_id=experiment_id
                )
                if final_ci.status != "passed":
                    keep_integration = True
                    return self._finish_failure(
                        experiment_id,
                        reason=(
                            f"CI on the integrated tree {published_sha[:12]} did not pass: "
                            f"{final_ci.status} {final_ci.reason or final_ci.conclusion or ''}".strip()
                        ),
                        actual_files=actual_files,
                        expected_files=expected_files,
                        test_exit_code=test_exit_code,
                        test_output=bounded_test_output,
                        experiment_commit_sha=experiment_commit_sha,
                        integrated_commit_sha=integrated_commit_sha,
                        diagnostic_worktree_path=str(integration_path),
                        elog=elog,
                    )

            plan_title = ""
            if isinstance(experiment.plan, dict):
                plan_title = str(experiment.plan.get("title") or "")
            pr_title = _collapse_summary(plan_title or experiment.goal, max_chars=200)
            pr_body = (
                f"Fixes #{work_ref.issue_number}\n\n"
                f"Nexus experiment: `{experiment_id}`\n\n"
                f"Goal: {_collapse_summary(experiment.goal, max_chars=1000)}\n\n"
                f"Verification: {verification_summary}; deterministic quality gate passed.\n\n"
                f"Changed files: {', '.join(actual_files[:30]) or '(none)'}"
            )
            try:
                work_ref = self.github_work_tracking.ensure_pull_request(
                    work_ref,
                    title=pr_title,
                    body=pr_body,
                    draft=False,
                )
                self.github_work_tracking.mark_pull_request_ready(work_ref)
                blocked = self._review_blocks(
                    experiment_id, experiment.goal, work_ref, integration_path, elog,
                    gate_findings="; ".join(dict.fromkeys(review_findings)),
                    model_written_tests=getattr(experiment, "model_written_tests", None) or "",
                )
                if blocked is not None:
                    return self._finish_failure(
                        experiment_id,
                        reason=blocked,
                        actual_files=actual_files,
                        expected_files=expected_files,
                        test_exit_code=test_exit_code,
                        test_output=bounded_test_output,
                        experiment_commit_sha=experiment_commit_sha,
                        integrated_commit_sha=integrated_commit_sha,
                        elog=elog,
                    )
                published_sha = self.github_work_tracking.merge_and_complete(
                    work_ref,
                    expected_head_sha=published_sha,
                ) or published_sha
            except MergeForbiddenError as exc:
                elog.warning(
                    "adoption", "merge_refused", str(exc),
                    pull_request_number=work_ref.pr_number,
                )
                return self._finish_review(
                    experiment_id,
                    reason=str(exc),
                    status=STATUS_READY_FOR_REVIEW,
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                )
            except GitHubWorkTrackingError as exc:
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason=f"GitHub PR konnte nicht automatisch abgeschlossen werden: {exc}",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            _run_git(
                ["fetch", "origin", _DEFAULT_TARGET_BRANCH],
                cwd=str(self.repo_root),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            local_sync_pending, adopted_reason = self._sync_local_repo(elog)
            self._set_status(
                experiment_id,
                STATUS_ADOPTED,
                ended_at=_utcnow_iso(),
                experiment_commit_sha=experiment_commit_sha,
                integrated_commit_sha=published_sha,
                expected_files_json=json.dumps(expected_files),
                actual_files_json=json.dumps(actual_files),
                test_exit_code=test_exit_code,
                test_output=bounded_test_output,
                reason=sanitized_reason(adopted_reason),
                local_sync_pending=1 if local_sync_pending else 0,
                diagnostic_worktree_path=None,
                updated_at=_utcnow_iso(),
            )
            self._report_issue_progress(
                experiment_id,
                "completed",
                f"Merged into {_DEFAULT_TARGET_BRANCH}"
                + (f" as {published_sha[:12]}." if published_sha else "."),
            )
            return self.get(experiment_id)
        except ExperimentAdoptionError as exc:
            return self._finish_failure(experiment_id, reason=str(exc), elog=elog)
        except subprocess.SubprocessError as exc:
            keep_integration = True
            return self._finish_failure(
                experiment_id,
                reason=f"Git-/Test-Prozess fehlgeschlagen: {exc}",
                diagnostic_worktree_path=str(integration_path) if integration_path else None,
                elog=elog,
            )
        finally:
            try:
                if integration_path is not None and integration_path.exists() and not keep_integration:
                    _run_git(
                        ["worktree", "remove", "--force", str(integration_path)],
                        cwd=str(self.repo_root),
                        timeout=120,
                        elog=elog,
                        phase="adoption",
                    )
                    shutil.rmtree(integration_path, ignore_errors=True)
            finally:
                lock.release()
