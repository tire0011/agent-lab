"""FailureAnalysis gathers current evidence through the shared tool path (#172).

The failure this covers is a real one: on the #115 / PR #116 rerun, a
persisted ``needs_review`` reason said ``GITHUB_TOKEN/GH_TOKEN`` was
unavailable, the diagnosis dutifully repeated that, and asked a human to
check a token the running process already had. Nothing was wrong with the
reasoning - it simply had no way to look. These tests pin down that it now
can, that it does so through the same ``find_tool``/``call_tool`` mechanism
the persistent controller uses, and that every guardrail around that access
holds.
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.failure_analysis_service import (
    CATEGORY_UNAVAILABLE,
    FailureAnalysisService,
    render_tool_line,
)
from modules.nexus_tool_router import (
    InMemoryToolGrantLedger,
    NexusToolRouter,
    ToolRouterError,
)
from modules.operations_registry import OperationResult, OperationSpec, OperationsRegistry
from modules.task_orchestrator import NexusTaskOrchestrator


def _read_only_specs(router):
    from modules.nexus_tool_router import is_read_only
    return [spec for spec in router.operations_registry.catalog() if is_read_only(spec)]

SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"

_STALE_FAILURE = (
    "GitHub lifecycle is required for Nexus implementation work. "
    "Configure GITHUB_TOKEN/GH_TOKEN before starting or resuming implementation."
)


def _diagnosis_json(**overrides) -> str:
    payload = {
        "summary": "no summary",
        "category": "unknown",
        "confidence": 0.9,
        "likely_root_causes": [],
        "missing_information": [],
        "recommended_checks": [],
        "recommended_actions": [],
        "can_self_recover": False,
        "needs_external_model": False,
        "needs_human": False,
        "escalation_reason": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


class _ScriptedClient:
    """Local-model stand-in that answers by prompt kind, not by call order.

    The evidence-gathering phase and the diagnosis share one job queue, so a
    positional script would silently mis-assign a response the moment the
    number of gathering steps changed. Dispatching on what the prompt *is*
    keeps each test's intent readable.
    """

    model = "test-local-model"

    def __init__(
        self,
        *,
        tool_steps: list[str],
        diagnosis: str,
        resolver_arguments: dict[str, dict] | None = None,
    ):
        self._tool_steps = list(tool_steps)
        self._diagnosis = diagnosis
        # Argument values this stand-in "reads out of" a goal, per resolved
        # operation - the resolver's job since #177, so a test can drive a
        # bound call, an invented value, or a goal that binds nothing.
        self._resolver_arguments = dict(resolver_arguments or {})
        self.prompts: list[str] = []
        self.tool_prompts: list[str] = []
        self.diagnosis_prompts: list[str] = []
        # What the script actually answered each gathering step with, so a
        # test can tell an LLM action decision from a deterministic one.
        self.tool_decisions: list[str] = []

    async def complete(self, *, prompt, temperature, max_tokens, system=None, response_format=None):
        del temperature, max_tokens, system, response_format
        self.prompts.append(prompt)
        if prompt.startswith("TOOL RESOLVER MODE."):
            # The shared router resolves capability lookups with its own
            # prompt; these tests drive find_tool through it for real.
            return self._resolve(prompt)
        if prompt.startswith("FAILURE ANALYSIS - EVIDENCE GATHERING STEP"):
            self.tool_prompts.append(prompt)
            decision = (
                self._tool_steps.pop(0)
                if self._tool_steps
                else json.dumps({"action": "done", "reason": "nothing left to check"})
            )
            self.tool_decisions.append(decision)
            return decision
        self.diagnosis_prompts.append(prompt)
        return self._diagnosis

    def _resolve(self, prompt: str) -> str:
        candidates = json.loads(prompt.split("CANDIDATE READ-ONLY OPERATIONS:\n", 1)[1].split("\n\nRules:", 1)[0])
        goal = prompt.split("GOAL:\n", 1)[1].split("\n\nCANDIDATE", 1)[0]
        for candidate in candidates:
            if candidate["operation"] in goal:
                found = {
                    "status": "found",
                    "operation": candidate["operation"],
                    "reason": "direct match",
                }
                arguments = self._resolver_arguments.get(candidate["operation"])
                if arguments is not None:
                    found["arguments"] = arguments
                return json.dumps(found)
        return json.dumps(
            {
                "status": "not_found",
                "operation": "",
                "reason": "no candidate satisfies the goal",
                "related_tools": [c["operation"] for c in candidates[:3]],
            }
        )


class _StubGitHubTracking:
    """A GitHub lifecycle that is ready right now, whatever was persisted."""

    def __init__(self, *, db_path: str, repo_root: str, token: str = SECRET):
        del db_path, repo_root
        self.repository = "tire0011/agent-lab"
        self.token = token
        self.target_branch = "main"

    @property
    def configured(self) -> bool:
        return bool(self.repository and self.token)

    def readiness_error(self) -> str | None:
        return None if self.configured else _STALE_FAILURE

    def close(self) -> None:
        return None


def _registry(tmp: Path, **kwargs) -> OperationsRegistry:
    return OperationsRegistry(
        experiment_service=SimpleNamespace(
            resolve_experiment=lambda experiment_id: SimpleNamespace(id=experiment_id)
        ),
        db_path=str(tmp / "agent.db"),
        repo_root=str(tmp),
        github_work_tracking_factory=kwargs.pop("github_work_tracking_factory", _StubGitHubTracking),
        **kwargs,
    )


def _service(tmp: Path, client, *, registry=None, max_tool_steps: int = 4) -> FailureAnalysisService:
    return FailureAnalysisService(
        db_path=str(tmp / "agent.db"),
        llama_client=client,
        operations_registry=registry if registry is not None else _registry(tmp),
        max_tool_steps=max_tool_steps,
    )


class SharedToolPathTests(unittest.TestCase):
    """The router failure analysis uses is the controller's router."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_analysis_discovers_and_calls_a_read_only_capability(self):
        client = _ScriptedClient(
            tool_steps=[
                json.dumps({
                    "action": "find_tool",
                    "goal": "check github_status lifecycle readiness",
                    "reason": "the recorded blocker is a configuration claim",
                }),
                json.dumps({
                    "action": "call_tool",
                    "tool": "github_status",
                    "arguments": {},
                    "reason": "read the current readiness",
                }),
                json.dumps({"action": "done", "reason": "verified"}),
            ],
            diagnosis=_diagnosis_json(summary="stale persisted blocker", category="lifecycle_logic"),
        )
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="mailbox_task",
            subject_id="task-115",
            failure=_STALE_FAILURE,
        )

        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertEqual(len(client.diagnosis_prompts), 1)
        diagnosis_prompt = client.diagnosis_prompts[0]
        self.assertIn("Current evidence, gathered just now", diagnosis_prompt)
        self.assertIn("[T1] find_tool", diagnosis_prompt)
        self.assertIn("[T2] call_tool", diagnosis_prompt)
        self.assertIn("lifecycle_ready: yes", diagnosis_prompt)
        # Tool evidence is citable the same way logged evidence is.
        self.assertIn("T1", analysis.evidence_refs)
        self.assertIn("T2", analysis.evidence_refs)

    def test_router_is_the_same_component_the_controller_drives(self):
        """One mechanism, two roles - not two mechanisms that look alike."""
        registry = _registry(self.tmp)
        self.addCleanup(registry.close)
        orchestrator = NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=registry.experiment_service,
            llama_client=_ScriptedClient(tool_steps=[], diagnosis="{}"),
            delegation_service=SimpleNamespace(catalog=lambda: [], close=lambda: None),
        )
        self.addCleanup(orchestrator.close)
        service = _service(self.tmp, _ScriptedClient(tool_steps=[], diagnosis=_diagnosis_json()), registry=registry)
        self.addCleanup(service.close)

        self.assertIsInstance(orchestrator.tool_router, NexusToolRouter)
        self.assertIsInstance(service._tool_router, NexusToolRouter)
        self.assertEqual(
            [spec.name for spec in _read_only_specs(orchestrator.tool_router)],
            [spec.name for spec in _read_only_specs(service._tool_router)],
        )

    def test_current_evidence_outranks_the_persisted_error(self):
        """The #115 case end to end: contradicted claim, evidence-led diagnosis."""
        client = _ScriptedClient(
            tool_steps=[
                json.dumps({
                    "action": "find_tool",
                    "goal": "check github_status lifecycle readiness",
                    "reason": "verify the token claim",
                }),
                json.dumps({
                    "action": "call_tool",
                    "tool": "github_status",
                    "arguments": {},
                    "reason": "read current readiness",
                }),
            ],
            diagnosis=_diagnosis_json(
                summary="the persisted blocker is stale; GitHub credentials are present",
                category="lifecycle_logic",
                confidence=0.82,
                likely_root_causes=[{
                    "cause": "stale persisted adoption/finalization state",
                    "confidence": 0.8,
                    "evidence": ["T2"],
                }],
            ),
        )
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="mailbox_task",
            subject_id="task-115",
            failure=_STALE_FAILURE,
        )

        prompt = client.diagnosis_prompts[0]
        # The recorded claim and the current fact are both in front of the
        # model, and the prompt says which one is newer.
        self.assertIn("Configure GITHUB_TOKEN/GH_TOKEN", prompt)
        self.assertIn("token_present: yes", prompt)
        self.assertIn("it is the newer fact", prompt)
        self.assertEqual(analysis.category, "lifecycle_logic")
        self.assertEqual(analysis.likely_root_causes[0].evidence, ("T2",))

    def test_operations_audit_records_the_diagnostic_call(self):
        """Audit logging is the registry's, reused - not re-implemented."""
        client = _ScriptedClient(
            tool_steps=[
                json.dumps({"action": "find_tool", "goal": "check github_status readiness", "reason": "verify"}),
                json.dumps({"action": "call_tool", "tool": "github_status", "arguments": {}, "reason": "read"}),
            ],
            diagnosis=_diagnosis_json(),
        )
        registry = _registry(self.tmp)
        self.addCleanup(registry.close)
        service = _service(self.tmp, client, registry=registry)
        self.addCleanup(service.close)

        service.analyze(subject_type="mailbox_task", subject_id="task-115", failure=_STALE_FAILURE)

        rows = registry._conn.execute(
            "SELECT operation FROM operations_audit ORDER BY rowid"
        ).fetchall()
        self.assertIn("github_status", [row["operation"] for row in rows])


class MissingCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_no_suitable_tool_keeps_the_analysis_valid_and_names_the_gap(self):
        client = _ScriptedClient(
            tool_steps=[
                json.dumps({
                    "action": "find_tool",
                    "goal": "inspect the GPU scheduler queue depth",
                    "reason": "no such capability exists",
                }),
            ],
            diagnosis=_diagnosis_json(
                summary="cannot confirm without GPU telemetry",
                category="missing_capability",
                confidence=0.5,
                missing_information=["GPU scheduler queue depth"],
            ),
        )
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="experiment",
            subject_id="exp-9",
            failure="the run stalled without an error",
        )

        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)
        prompt = client.diagnosis_prompts[0]
        self.assertIn("No Nexus capability could supply evidence for", prompt)
        self.assertIn("[T1] find_tool", prompt)
        self.assertIn("status=not_found", prompt)
        self.assertEqual(analysis.missing_information, ("GPU scheduler queue depth",))

    def test_without_a_tool_router_the_prompt_says_nothing_was_verified(self):
        client = _ScriptedClient(tool_steps=[], diagnosis=_diagnosis_json())
        service = FailureAnalysisService(
            db_path=str(self.tmp / "agent.db"), llama_client=client
        )
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="mailbox_task", subject_id="task-1", failure=_STALE_FAILURE
        )

        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertEqual(client.tool_prompts, [])
        self.assertIn(
            "No read-only diagnostic tools were available", client.diagnosis_prompts[0]
        )

    def test_call_tool_without_a_grant_is_refused(self):
        """The controller's rule, enforced for this role too."""
        client = _ScriptedClient(
            tool_steps=[
                json.dumps({"action": "call_tool", "tool": "github_status", "arguments": {}, "reason": "skip discovery"}),
            ],
            diagnosis=_diagnosis_json(),
        )
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        service.analyze(subject_type="mailbox_task", subject_id="task-1", failure=_STALE_FAILURE)

        prompt = client.diagnosis_prompts[0]
        self.assertIn("error=operation_not_granted", prompt)
        self.assertNotIn("lifecycle_ready", prompt)

    def test_write_operations_are_not_reachable(self):
        """Diagnosis is read-only by construction, not by instruction."""
        registry = _registry(self.tmp)
        self.addCleanup(registry.close)
        router = NexusToolRouter(
            operations_registry=registry,
            schema_completion=None,
            grant_ledger=InMemoryToolGrantLedger(),
            caller="failure analysis",
        )
        self.assertNotIn(
            "inbox_refresh", [spec.name for spec in _read_only_specs(router)]
        )
        with self.assertRaises(ToolRouterError):
            router.execute_operation(
                {"operation": "inbox_refresh", "args": {}, "session_id": "analysis-1"}
            )


class ReadOnlyBoundaryDurabilityTests(unittest.TestCase):
    """The boundary has to hold for operations nobody has written yet.

    Defining read-only as "the catalog minus a denylist" made a capability
    added later read-only by default, so this role's privileges would grow
    whenever the catalog did and nobody extended the list. These tests use
    operations that are not in the real catalog at all, and never touch a
    central list - if the boundary regresses to a denylist, they fail.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _registry_with(self, *extra_specs):
        class _GrownRegistry(OperationsRegistry):
            def catalog(inner):
                return list(super().catalog()) + list(extra_specs)

            def _op_merge_pull_request(inner, args):
                raise AssertionError("a read-only role must never reach this handler")

            def _op_future_operation(inner, args):
                raise AssertionError("a read-only role must never reach this handler")

        return _GrownRegistry(
            experiment_service=SimpleNamespace(
                resolve_experiment=lambda experiment_id: SimpleNamespace(id=experiment_id)
            ),
            db_path=str(self.tmp / "agent.db"),
            repo_root=str(self.tmp),
            github_work_tracking_factory=_StubGitHubTracking,
        )

    def _router(self, registry):
        return NexusToolRouter(
            operations_registry=registry,
            schema_completion=None,
            grant_ledger=InMemoryToolGrantLedger(),
            caller="failure analysis",
        )

    def test_a_spec_must_declare_its_access_to_exist(self):
        with self.assertRaises(TypeError):
            OperationSpec("merge_pull_request", "Merge the pull request")

    def test_newly_added_mutating_operation_is_neither_visible_nor_callable(self):
        mutating = OperationSpec(
            "merge_pull_request",
            "Merge the pull request for an experiment.",
            tags=["github", "merge", "pull request", "lifecycle", "readiness"],
            mutates_state=True,
        )
        registry = self._registry_with(mutating)
        self.addCleanup(registry.close)
        router = self._router(registry)

        # It is in the catalog the controller-facing registry exposes...
        self.assertIn("merge_pull_request", [spec.name for spec in registry.catalog()])
        # ...and absent from the surface a read-only role can see.
        self.assertNotIn(
            "merge_pull_request",
            [spec.name for spec in _read_only_specs(router)],
        )
        # find_tool cannot even offer it as a candidate, so the resolver is
        # never given the chance to pick it.
        self.assertNotIn(
            "merge_pull_request",
            [spec.name for spec in router.candidate_operations("merge the pull request")],
        )
        with self.assertRaises(ToolRouterError):
            router.execute_operation(
                {"operation": "merge_pull_request", "args": {}, "session_id": "analysis-1"}
            )

    def test_a_forged_grant_does_not_make_a_mutating_operation_callable(self):
        """Access is checked against the declaration, not against the grant."""
        mutating = OperationSpec(
            "merge_pull_request", "Merge the pull request.", mutates_state=True
        )
        registry = self._registry_with(mutating)
        self.addCleanup(registry.close)
        router = self._router(registry)
        router.grant_ledger.record_grant(
            "analysis-1", {"status": "found", "operation": mutating.to_dict()}
        )

        with self.assertRaises(ToolRouterError):
            router.execute_operation(
                {"operation": "merge_pull_request", "args": {}, "session_id": "analysis-1"}
            )

    def test_a_spec_that_declares_nothing_is_treated_as_mutating(self):
        """Duck-typed specs fail closed too, not just OperationSpec ones."""
        undeclared = SimpleNamespace(
            name="future_operation",
            description="Something a later change added without deciding.",
            arguments=[],
            tags=["future", "operation", "readiness"],
            to_dict=lambda: {"operation": "future_operation", "description": "", "arguments": [], "tags": []},
        )
        registry = self._registry_with(undeclared)
        self.addCleanup(registry.close)
        router = self._router(registry)

        self.assertNotIn(
            "future_operation",
            [spec.name for spec in _read_only_specs(router)],
        )
        with self.assertRaises(ToolRouterError):
            router.execute_operation(
                {"operation": "future_operation", "args": {}, "session_id": "analysis-1"}
            )

    def test_the_controller_sees_the_same_boundary(self):
        """One boundary, not a diagnosis-only one the controller can bypass."""
        mutating = OperationSpec(
            "merge_pull_request", "Merge the pull request.", mutates_state=True
        )
        registry = self._registry_with(mutating)
        self.addCleanup(registry.close)
        orchestrator = NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=registry.experiment_service,
            llama_client=_ScriptedClient(tool_steps=[], diagnosis="{}"),
            delegation_service=SimpleNamespace(catalog=lambda: [], close=lambda: None),
        )
        self.addCleanup(orchestrator.close)

        self.assertNotIn(
            "merge_pull_request",
            [spec.name for spec in orchestrator._read_only_operation_specs()],
        )

    def test_every_real_capability_declares_its_access(self):
        registry = _registry(self.tmp)
        self.addCleanup(registry.close)
        for spec in registry.catalog():
            with self.subTest(operation=spec.name):
                self.assertIsInstance(spec.mutates_state, bool)
                self.assertEqual(spec.read_only, not spec.mutates_state)
        self.assertEqual(
            sorted(spec.name for spec in registry.catalog() if spec.mutates_state),
            ["github_mcp_call", "inbox_refresh", "install_tool", "machine_reboot", "request_from_owner", "service_restart"],
        )


class BoundedToolUseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_identical_find_tool_lookups_are_bounded(self):
        repeat = json.dumps({
            "action": "find_tool",
            "goal": "inspect the GPU scheduler queue depth",
            "reason": "same lookup again",
        })
        client = _ScriptedClient(tool_steps=[repeat] * 8, diagnosis=_diagnosis_json())
        service = _service(self.tmp, client, max_tool_steps=6)
        self.addCleanup(service.close)

        service.analyze(subject_type="experiment", subject_id="exp-1", failure="stalled")

        prompt = client.diagnosis_prompts[0]
        tool_lines = [line for line in prompt.splitlines() if line.startswith("[T")]
        # The lookup resolved exactly once; every identical repeat was refused
        # by the shared loop guard rather than resolved again.
        resolved = [line for line in tool_lines if "status=refused" not in line]
        self.assertEqual(len(resolved), 1, tool_lines)
        # A refusal is reported back instead of being swallowed, so the model
        # can spend the remaining budget on something else (#175).
        refused = [line for line in tool_lines if "status=refused" in line]
        self.assertTrue(refused, tool_lines)
        self.assertIn("Ask for different evidence", refused[0])
        # A model that keeps repeating anyway is still bounded.
        self.assertLessEqual(len(client.tool_prompts), 6)

    def test_a_refused_repeat_leaves_the_rest_of_the_budget_usable(self):
        """Regression for #175: one repeat must not end the whole phase.

        In the live analysis ``878e8008`` the model repeated its opening
        ``find_tool``; the guard was configured so that the *first* repeat was
        terminal, the loop broke out with a single observation, and 2 of 4
        steps went unused. The diagnosis was then written almost entirely from
        40 historical log lines and reproduced an obsolete token error.
        """
        client = _ScriptedClient(
            tool_steps=[
                json.dumps({
                    "action": "find_tool",
                    "goal": "check github_status lifecycle readiness",
                    "reason": "the recorded blocker is a configuration claim",
                }),
                # The repeat that used to end everything.
                json.dumps({
                    "action": "find_tool",
                    "goal": "check github_status lifecycle readiness",
                    "reason": "asking again",
                }),
                json.dumps({
                    "action": "call_tool",
                    "tool": "github_status",
                    "arguments": {},
                    "reason": "read the current readiness",
                }),
                json.dumps({"action": "done", "reason": "verified"}),
            ],
            diagnosis=_diagnosis_json(summary="stale persisted blocker", category="lifecycle_logic"),
        )
        service = _service(self.tmp, client, max_tool_steps=4)
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="mailbox_task",
            subject_id="task-175",
            failure=_STALE_FAILURE,
        )

        prompt = client.diagnosis_prompts[0]
        # The call after the refusal still ran, and its evidence is what the
        # diagnosis was written from.
        self.assertIn("call_tool", prompt)
        self.assertIn("lifecycle_ready: yes", prompt)
        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)

    def test_the_refusal_is_visible_to_the_model_in_the_next_step(self):
        client = _ScriptedClient(
            tool_steps=[
                json.dumps({"action": "find_tool", "goal": "check github_status readiness", "reason": "verify"}),
                json.dumps({"action": "find_tool", "goal": "check github_status readiness", "reason": "again"}),
                json.dumps({"action": "done", "reason": "enough"}),
            ],
            diagnosis=_diagnosis_json(),
        )
        service = _service(self.tmp, client, max_tool_steps=4)
        self.addCleanup(service.close)

        service.analyze(subject_type="experiment", subject_id="exp-175", failure="stalled")

        # Step 3's prompt has to carry the refusal, otherwise it looks
        # identical to step 2's and the model repeats itself again.
        self.assertGreaterEqual(len(client.tool_prompts), 3)
        self.assertIn("status=refused", client.tool_prompts[2])
        self.assertIn("Ask for different evidence", client.tool_prompts[2])

    def test_step_budget_caps_the_gathering_phase(self):
        steps = [
            json.dumps({
                "action": "find_tool",
                "goal": f"inspect subsystem {index} health",
                "reason": "exploring",
            })
            for index in range(10)
        ]
        client = _ScriptedClient(tool_steps=steps, diagnosis=_diagnosis_json())
        service = _service(self.tmp, client, max_tool_steps=2)
        self.addCleanup(service.close)

        service.analyze(subject_type="experiment", subject_id="exp-2", failure="stalled")

        self.assertEqual(len(client.tool_prompts), 2)
        self.assertEqual(len(client.diagnosis_prompts), 1)

    def test_one_analysis_budget_does_not_spend_another(self):
        def make_client():
            return _ScriptedClient(
                tool_steps=[
                    json.dumps({"action": "find_tool", "goal": "check github_status readiness", "reason": "verify"}),
                    json.dumps({"action": "call_tool", "tool": "github_status", "arguments": {}, "reason": "read"}),
                ],
                diagnosis=_diagnosis_json(),
            )

        registry = _registry(self.tmp)
        self.addCleanup(registry.close)
        for subject_id in ("task-a", "task-b"):
            client = make_client()
            service = _service(self.tmp, client, registry=registry)
            try:
                service.analyze(
                    subject_type="mailbox_task", subject_id=subject_id, failure=_STALE_FAILURE
                )
            finally:
                service.close()
            self.assertIn("lifecycle_ready: yes", client.diagnosis_prompts[0], subject_id)


class ToolFailureContainmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_failing_operation_does_not_break_or_recurse_the_analysis(self):
        class _ExplodingRegistry(OperationsRegistry):
            def execute(self, operation, args=None, *, caller="authenticated caller"):
                raise RuntimeError("registry exploded")

        registry = _ExplodingRegistry(
            experiment_service=SimpleNamespace(
                resolve_experiment=lambda experiment_id: SimpleNamespace(id=experiment_id)
            ),
            db_path=str(self.tmp / "agent.db"),
            repo_root=str(self.tmp),
        )
        self.addCleanup(registry.close)
        client = _ScriptedClient(
            tool_steps=[
                json.dumps({"action": "find_tool", "goal": "check github_status readiness", "reason": "verify"}),
                json.dumps({"action": "call_tool", "tool": "github_status", "arguments": {}, "reason": "read"}),
            ],
            diagnosis=_diagnosis_json(summary="diagnosis still produced", confidence=0.7),
        )
        service = _service(self.tmp, client, registry=registry)
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="mailbox_task", subject_id="task-1", failure=_STALE_FAILURE
        )

        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertEqual(analysis.summary, "diagnosis still produced")
        # Exactly one analysis record: a tool failure never queues another.
        self.assertEqual(len(service.list_recent(limit=10)), 1)

    def test_unparseable_tool_decision_falls_through_to_the_diagnosis(self):
        client = _ScriptedClient(
            tool_steps=["I would like to check the token, please."],
            diagnosis=_diagnosis_json(summary="diagnosed from logged evidence only"),
        )
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="mailbox_task", subject_id="task-1", failure=_STALE_FAILURE
        )

        self.assertEqual(analysis.summary, "diagnosed from logged evidence only")
        self.assertIn("No current evidence was gathered", client.diagnosis_prompts[0])


class ToolOutputRedactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _leaky_registry(self):
        class _LeakyRegistry(OperationsRegistry):
            def execute(self, operation, args=None, *, caller="authenticated caller"):
                return OperationResult(
                    operation=operation,
                    success=True,
                    output_lines=[f"token: {SECRET}"] + [f"filler line {i} " + "x" * 400 for i in range(40)],
                    duration_ms=1,
                )

        return _LeakyRegistry(
            experiment_service=SimpleNamespace(
                resolve_experiment=lambda experiment_id: SimpleNamespace(id=experiment_id)
            ),
            db_path=str(self.tmp / "agent.db"),
            repo_root=str(self.tmp),
        )

    def test_secret_never_reaches_prompt_persistence_logs_or_outbox(self):
        registry = self._leaky_registry()
        self.addCleanup(registry.close)
        client = _ScriptedClient(
            tool_steps=[
                json.dumps({"action": "find_tool", "goal": "check github_status readiness", "reason": "verify"}),
                json.dumps({"action": "call_tool", "tool": "github_status", "arguments": {}, "reason": "read"}),
            ],
            # A model that echoes the leak back is exactly the case the
            # result-side bounds have to survive.
            diagnosis=_diagnosis_json(summary=f"token is {SECRET}"),
        )
        outbox = self.tmp / "outbox"
        outbox.mkdir()
        service = FailureAnalysisService(
            db_path=str(self.tmp / "agent.db"),
            llama_client=client,
            operations_registry=registry,
            outbox_dir=outbox,
            repo_root=self.tmp,
        )
        self.addCleanup(service.close)

        with self.assertLogs(level=logging.DEBUG) as captured:
            analysis = service.analyze(
                subject_type="mailbox_task",
                subject_id="task-1",
                mailbox_task_id="task-1",
                failure=_STALE_FAILURE,
            )

        for prompt in client.prompts:
            self.assertNotIn(SECRET, prompt)
        self.assertNotIn(SECRET, json.dumps(analysis.to_dict()))
        self.assertNotIn(SECRET, json.dumps(service.get(analysis.id)))
        self.assertNotIn(SECRET, "\n".join(captured.output))
        published = list(outbox.glob("*.md"))
        self.assertEqual(len(published), 1)
        self.assertNotIn(SECRET, published[0].read_text(encoding="utf-8"))

    def test_tool_output_is_length_bounded_before_it_reaches_the_model(self):
        registry = self._leaky_registry()
        self.addCleanup(registry.close)
        client = _ScriptedClient(
            tool_steps=[
                json.dumps({"action": "find_tool", "goal": "check github_status readiness", "reason": "verify"}),
                json.dumps({"action": "call_tool", "tool": "github_status", "arguments": {}, "reason": "read"}),
            ],
            diagnosis=_diagnosis_json(),
        )
        service = _service(self.tmp, client, registry=registry)
        self.addCleanup(service.close)

        service.analyze(subject_type="mailbox_task", subject_id="task-1", failure=_STALE_FAILURE)

        tool_line = next(
            line for line in client.diagnosis_prompts[0].splitlines() if line.startswith("[T2]")
        )
        # 12 lines at most, 200 chars each, plus the small key=value framing.
        self.assertLess(len(tool_line), 12 * 200 + 400)
        self.assertLessEqual(tool_line.count(" | "), 11)


class RenderingTests(unittest.TestCase):
    def test_tool_line_matches_the_evidence_line_vocabulary(self):
        line = render_tool_line(
            3, {"action": "call_tool", "tool": "github_status", "status": "ok", "output": "a | b"}
        )
        self.assertEqual(line, "[T3] call_tool status=ok tool=github_status output=a | b")


if __name__ == "__main__":
    unittest.main()
