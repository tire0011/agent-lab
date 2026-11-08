"""Worktree integration: snapshots, commits, conflicts and start validation of :class:`ExperimentAdoptionService`."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid

from pathlib import Path
from typing import Any

from modules.experiment_evaluation import parse_git_status_short
from modules.experiment_service import (
    STATUS_SUCCEEDED,
    _run_git,
)

from modules.experiment_adoption_parts.core import (
    ExperimentAdoptionError,
    _DEFAULT_GIT_TIMEOUT_SECONDS,
    _DEFAULT_TARGET_BRANCH,
    _collapse_summary,
    _normalize_path,
    _safe_list,
)

logger = logging.getLogger("modules.experiment_adoption_service")

class IntegrationMixin:
    """Worktree integration: snapshots, commits, conflicts and start validation of :class:`ExperimentAdoptionService`."""

    def _validate_startable(self, experiment_id: str, *, persist_review: bool) -> dict[str, Any]:
        experiment = self.experiment_service.get_experiment(experiment_id)
        if experiment.status != STATUS_SUCCEEDED:
            raise ExperimentAdoptionError("Nur erfolgreiche Experimente können übernommen werden.")
        quality = experiment.quality_evaluation or {}
        if quality.get("outcome") != "pass":
            raise ExperimentAdoptionError("Das Qualitäts-Gate des Experiments ist nicht bestanden.")
        worktree_path = experiment.worktree_path or ""
        if not worktree_path or not os.path.isdir(worktree_path):
            raise ExperimentAdoptionError("Der Experiment-Worktree ist nicht mehr verfügbar.")

        elog = self.experiment_service.get_experiment_logger(experiment_id)
        snapshot = self._capture_snapshot(worktree_path, elog, phase="adoption")
        current_status = (snapshot["git_status"] or "").strip()
        stored_status = (experiment.git_status or "").strip()
        current_stat = (snapshot["diff_stat"] or "").strip()
        stored_stat = (experiment.diff_stat or "").strip()
        if current_status != stored_status or current_stat != stored_stat:
            # Compare content, not arrangement: git status/diff strings change once
            # adoption commits the verified changes, which made a resumed adoption
            # accuse itself of tampering (#147 follow-up). The invariant: is the
            # tree adoption would integrate byte-for-byte the tree CI verified?
            checkpoint_sha = self._verified_checkpoint_sha(experiment_id, experiment)
            verified_tree = self._tree_sha_of(worktree_path, checkpoint_sha, elog, phase="adoption")
            current_tree = (snapshot.get("tree_sha") or "").strip()
            if verified_tree and current_tree and verified_tree == current_tree:
                elog.info(
                    "adoption", "worktree_content_matches_checkpoint",
                    "Worktree layout changed since verification, but its content is "
                    "identical to the verified checkpoint; continuing adoption",
                    checkpoint_sha=checkpoint_sha,
                    tree_sha=current_tree,
                )
            else:
                raise ExperimentAdoptionError(
                    "Die Änderungen im Experiment-Worktree haben sich seit der erfolgreichen Prüfung verändert."
                )

        actual_files = _safe_list(snapshot["changed"].all_paths())
        # An empty uncommitted diff is not an empty change: the verified content
        # may already be committed. Decide here, before the pipeline spends a
        # bounded retry attempt on a retry that cannot succeed (#175).
        committed_commit_sha: str | None = None
        if not actual_files:
            committed_commit_sha, actual_files = self._committed_verified_work(
                experiment_id, experiment, worktree_path, elog
            )
            if committed_commit_sha is None:
                raise ExperimentAdoptionError("Keine Änderungen zur Übernahme vorhanden.")
            elog.info(
                "adoption",
                "verified_work_already_committed",
                "The worktree is clean because the verified content is already "
                "committed; adopting that commit instead of an uncommitted diff",
                experiment_commit_sha=committed_commit_sha,
                changed_files=actual_files,
            )
        expected_files = _safe_list((quality.get("changed_files") or quality.get("expected_files") or actual_files))
        review_reason = self._review_reason(actual_files, expected_files)
        if review_reason is not None:
            if persist_review:
                self._update_review_state(
                    experiment_id,
                    review_reason,
                    actual_files=actual_files,
                    expected_files=expected_files,
                )
            raise ExperimentAdoptionError(review_reason)

        return {
            "experiment": experiment,
            "actual_files": actual_files,
            "expected_files": expected_files,
            "snapshot": snapshot,
            "committed_commit_sha": committed_commit_sha,
        }


    def _capture_snapshot(self, worktree_path: str, elog, *, phase: str) -> dict[str, Any]:
        status_proc = _run_git(
            ["status", "--short", "--untracked-files=all"],
            cwd=worktree_path,
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
            elog=elog,
            phase=phase,
        )
        scratch_dir = self.worktrees_root / f"adoption-index-{uuid.uuid4().hex}"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        index_env = dict(os.environ)
        index_env["GIT_INDEX_FILE"] = str(scratch_dir / "index")
        try:
            read_tree = _run_git(
                ["read-tree", "HEAD"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            add_proc = _run_git(
                ["add", "-A"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            if read_tree.returncode != 0 or add_proc.returncode != 0:
                failed = read_tree if read_tree.returncode != 0 else add_proc
                raise subprocess.CalledProcessError(failed.returncode, failed.args, output=failed.stdout, stderr=failed.stderr)
            stat_proc = _run_git(
                ["diff", "--cached", "--stat", "HEAD"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            diff_proc = _run_git(
                ["diff", "--cached", "HEAD"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            # The scratch index holds HEAD plus every worktree change, so its tree
            # is exactly what adoption would integrate; compare that against the
            # verified checkpoint regardless of where the content currently sits.
            tree_proc = _run_git(
                ["write-tree"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        return {
            "git_status": status_proc.stdout,
            "diff_stat": stat_proc.stdout,
            "diff_text": diff_proc.stdout,
            "changed": parse_git_status_short(status_proc.stdout),
            "tree_sha": tree_proc.stdout.strip() if tree_proc.returncode == 0 else "",
        }


    def _committed_verified_work(
        self, experiment_id: str, experiment: Any, worktree_path: str, elog
    ) -> tuple[str | None, list[str]]:
        """``HEAD`` and its changed paths when the verified content is already committed.

        Adoption commits the verified changes first, so an attempt that got that
        far leaves a clean worktree with the content in ``HEAD`` - reading only
        the uncommitted diff then yields nothing (#175). The content invariant
        answers without trusting any record: if ``HEAD`` carries exactly the tree
        verification accepted and differs from the target branch, ``HEAD`` *is*
        the change to adopt. Raises when the comparison could not be made - an
        unanswerable question must not read as an empty answer.
        """
        checkpoint_sha = self._verified_checkpoint_sha(experiment_id, experiment)
        verified_tree = self._tree_sha_of(worktree_path, checkpoint_sha, elog, phase="adoption")
        head_sha = self._git_stdout(["rev-parse", "HEAD"], worktree_path, elog).strip()
        head_tree = self._tree_sha_of(worktree_path, head_sha, elog, phase="adoption")
        if not verified_tree or not head_tree or verified_tree != head_tree:
            return None, []
        # "Nothing to adopt" may only be concluded against a target branch that
        # actually resolved; an unreachable ref must not read as an empty change.
        target_ref = f"origin/{_DEFAULT_TARGET_BRANCH}"
        resolved_target = _run_git(
            ["rev-parse", "--verify", f"{target_ref}^{{commit}}"],
            cwd=worktree_path,
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
            elog=elog,
            phase="adoption",
        )
        if resolved_target.returncode != 0:
            raise ExperimentAdoptionError(
                f"Der Ziel-Branch {target_ref} ist im Experiment-Worktree nicht auflösbar."
            )
        # Three dots: what this experiment added since it diverged from the target
        # branch, not whatever else landed on the target meanwhile.
        changed = _run_git(
            ["diff", "--name-only", f"{target_ref}...HEAD"],
            cwd=worktree_path,
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
            elog=elog,
            phase="adoption",
        )
        if changed.returncode != 0:
            raise ExperimentAdoptionError(
                "Der Experiment-Commit konnte nicht mit dem Ziel-Branch verglichen werden."
            )
        changed_files = _safe_list(
            [line.strip() for line in changed.stdout.splitlines() if line.strip()]
        )
        if not changed_files:
            return None, []
        return head_sha, changed_files


    def _resumable_commit_sha(self, experiment_id: str) -> str | None:
        record = self.get(experiment_id)
        recorded = (record.experiment_commit_sha or "").strip()
        if not recorded:
            return None
        try:
            experiment = self.experiment_service.get_experiment(experiment_id)
        except Exception:
            return None
        worktree_path = getattr(experiment, "worktree_path", "") or ""
        if not worktree_path or not os.path.isdir(worktree_path):
            return None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if head.returncode != 0:
            return None
        return recorded if head.stdout.strip() == recorded else None


    def _verified_checkpoint_sha(self, experiment_id: str, experiment: Any) -> str:
        """Return the commit whose content verification actually accepted: the
        GitHub work ref's checkpoint, else the SHA verification recorded in
        ``test_command`` as ``["GitHub Actions", <workflow>, <sha>]``."""
        try:
            work_ref = self.github_work_tracking.for_experiment(experiment_id)
        except Exception:
            work_ref = None
        sha = (getattr(work_ref, "checkpoint_sha", None) or "").strip() if work_ref is not None else ""
        if sha:
            return sha
        command = getattr(experiment, "test_command", None) or []
        if len(command) >= 3 and str(command[0]).strip() == "GitHub Actions":
            return str(command[2]).strip()
        return ""


    def _tree_sha_of(self, worktree_path: str, commit_sha: str, elog, *, phase: str) -> str:
        if not commit_sha:
            return ""
        proc = _run_git(
            ["rev-parse", f"{commit_sha}^{{tree}}"],
            cwd=worktree_path,
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
            elog=elog,
            phase=phase,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""


    def _integration_base_sha(self) -> str:
        """The target-branch commit adoption would cherry-pick onto, read from the
        local ``origin/<target>`` ref so it works without credentials and matches
        the base adoption actually uses; empty when unresolvable, which callers
        treat as "cannot prove the base is unchanged"."""
        proc = _run_git(
            ["rev-parse", f"origin/{_DEFAULT_TARGET_BRANCH}"],
            cwd=str(self.repo_root),
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            logger.warning(
                "could not resolve the integration base for %s", _DEFAULT_TARGET_BRANCH
            )
            return ""
        return (proc.stdout or "").strip()


    def _review_reason(self, actual_files: list[str], expected_files: list[str]) -> str | None:
        for path in actual_files:
            reason = self._denied_path_reason(path)
            if reason is not None:
                return reason
        if expected_files:
            extras = [path for path in actual_files if path not in expected_files]
            if extras:
                return "Unerwartete Dateien im Experiment-Diff; manuelle Prüfung nötig."
        return None


    def _denied_path_reason(self, path: str) -> str | None:
        normalized = _normalize_path(path)
        lower = normalized.lower()
        if not normalized:
            return "Leere Dateipfade im Experiment-Diff sind unzulässig."
        parts = Path(normalized).parts
        if normalized.startswith("/") or ".." in parts:
            return "Dateien außerhalb des Repositories erfordern manuelle Prüfung."
        if lower.startswith(".git/") or "/.git/" in lower:
            return "Änderungen an Git-Metadaten erfordern manuelle Prüfung."
        if lower.startswith("communication/outbox/"):
            return "Änderungen unter communication/outbox/ werden nicht automatisch übernommen."
        if lower == ".env" or lower.endswith("/.env") or lower.endswith(".pem"):
            return "Sensible Konfigurationsdateien werden nicht automatisch übernommen."
        # Test files may be named after what they test ("print_vault_secrets.md"
        # fixtures); the reviewer reads their content.
        if ("secret" in lower or "credential" in lower) and not lower.startswith("tests/"):
            return "Potentiell sensible Dateien werden nicht automatisch übernommen."
        return None


    def _git_stdout(self, args: list[str], cwd: str, elog) -> str:
        proc = _run_git(args, cwd=cwd, timeout=_DEFAULT_GIT_TIMEOUT_SECONDS, elog=elog, phase="adoption")
        return proc.stdout or ""


    def _commit_message(self, experiment_id: str, goal: str, plan: dict[str, Any] | None) -> str:
        title = ""
        if isinstance(plan, dict):
            title = str(plan.get("title") or "")
        summary = _collapse_summary(title or goal)
        return f"Adopt experiment {experiment_id[:8]}: {summary}"


    def _sync_local_repo(self, elog) -> tuple[bool, str]:
        status_proc = _run_git(["status", "--porcelain"], cwd=str(self.repo_root), timeout=30, elog=elog, phase="adoption")
        if status_proc.returncode != 0 or status_proc.stdout.strip():
            return True, "Übernahme veröffentlicht; lokales Repo ist nicht sauber und muss manuell synchronisiert werden."
        branch_proc = _run_git(["branch", "--show-current"], cwd=str(self.repo_root), timeout=30, elog=elog, phase="adoption")
        if branch_proc.returncode != 0 or branch_proc.stdout.strip() != _DEFAULT_TARGET_BRANCH:
            return True, "Übernahme veröffentlicht; lokales Repo ist nicht auf main und wurde nicht automatisch synchronisiert."
        merge_proc = _run_git(["merge", "--ff-only", f"origin/{_DEFAULT_TARGET_BRANCH}"], cwd=str(self.repo_root), timeout=120, elog=elog, phase="adoption")
        if merge_proc.returncode != 0:
            return True, "Übernahme veröffentlicht; lokales Repo konnte nicht per Fast-Forward synchronisiert werden."
        return False, "Experiment erfolgreich übernommen und veröffentlicht."
