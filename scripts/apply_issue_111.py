from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "modules" / "task_orchestrator.py"
TEST_MAIN = ROOT / "tests" / "test_task_orchestrator.py"
TEST_COMMAND = ROOT / "tests" / "test_task_orchestrator_command_tool.py"
NEW_TEST = ROOT / "tests" / "test_issue_111_controller_protocol.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def regex_once(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return updated


text = ORCH.read_text(encoding="utf-8")

text = replace_once(
    text,
    'TOOL_FIND_TOOL = "find_tool"\nTOOL_PYTHON_RUN = "python_run"',
    'TOOL_FIND_TOOL = "find_tool"\nACTION_CALL_TOOL = "call_tool"\nTOOL_PYTHON_RUN = "python_run"',
    "call_tool constant",
)

text = replace_once(
    text,
    '''_ORCHESTRATOR_TOOLS = {\n    TOOL_FIND_TOOL,\n    "operation",\n    "memory_search",\n    "delegate",\n    ACTION_COMMAND,\n    TOOL_PYTHON_RUN,\n}''',
    '''_ORCHESTRATOR_TOOLS = {\n    TOOL_FIND_TOOL,\n    ACTION_CALL_TOOL,\n    "memory_search",\n    "delegate",\n    ACTION_COMMAND,\n    TOOL_PYTHON_RUN,\n}''',
    "intermediate action set",
)

new_schema = '''_ORCHESTRATOR_RESPONSE_FORMAT: dict[str, Any] = {\n    "type": "json_schema",\n    "schema": {\n        "type": "object",\n        "properties": {\n            "action": {\n                "type": "string",\n                "enum": [\n                    TOOL_FIND_TOOL,\n                    ACTION_CALL_TOOL,\n                    "memory_search",\n                    "delegate",\n                    ACTION_COMMAND,\n                    TOOL_PYTHON_RUN,\n                    ACTION_NEW_EXPERIMENT,\n                    ACTION_RESUME_EXPERIMENT,\n                    ACTION_ANSWER,\n                    ACTION_MISSING_CAPABILITY,\n                    ACTION_CLARIFICATION,\n                ],\n            },\n            "goal": {"type": "string"},\n            "tool": {"type": "string"},\n            "arguments": {"type": "object", "additionalProperties": True},\n            "query": {"type": "string"},\n            "limit": {"type": "integer"},\n            "provider": {"type": "string"},\n            "task": {"type": "string"},\n            "context": {"type": "string"},\n            "command": {"type": "string"},\n            "script": {"type": "string"},\n            "module": {"type": "string"},\n            "argv": {"type": "array", "items": {"type": "string"}},\n            "cwd": {"type": "string"},\n            "experiment_id": {"type": ["string", "null"]},\n            "message": {"type": "string"},\n            "reason": {"type": "string"},\n        },\n        "required": ["action"],\n        "additionalProperties": False,\n    },\n}\n\n'''
text = regex_once(
    text,
    r'_ORCHESTRATOR_RESPONSE_FORMAT: dict\[str, Any\] = \{.*?\n\}\n\n(?=_TOOL_RESOLVER_RESPONSE_FORMAT)',
    new_schema,
    "controller schema",
)

run_block = '''            action = str(decision.get("action", "")).strip().lower()\n            if action in _ORCHESTRATOR_TOOLS:\n                execution_decision = self._action_to_tool_decision(decision)\n                if task_logger is not None:\n                    task_logger.info(\n                        "orchestration",\n                        "tool_selected",\n                        (\n                            "orchestrator selected intermediate action "\n                            f"{action}: {decision.get('reason', '')}"\n                        ),\n                        tool=action,\n                        reason=str(decision.get("reason", "")),\n                        session_id=session.id,\n                    )\n                tool, arguments = self._tool_identity(execution_decision)\n                check = self.loop_guard.check(\n                    session.id,\n                    tool,\n                    arguments,\n                    state_token=state_token,\n                    reason=str(decision.get("reason", "")),\n                )\n                if not check.allowed:\n                    observation = check.observation or {"event": "duplicate_tool_call"}\n                    logger.warning(\n                        "tool loop detected session_id=%s signature=%s previous_call_id=%s recovery=%s",\n                        session.id,\n                        check.signature,\n                        observation.get("previous_call_id"),\n                        observation.get("recovery"),\n                        extra={"session_id": session.id, "event": "tool_loop_detected"},\n                    )\n                    if task_logger is not None:\n                        task_logger.warning(\n                            "orchestration",\n                            str(observation.get("event", "duplicate_tool_call")),\n                            (\n                                f"tool loop detected signature={check.signature} "\n                                f"previous_call_id={observation.get('previous_call_id')} "\n                                f"recovery={observation.get('recovery')}"\n                            ),\n                            session_id=session.id,\n                            signature=check.signature,\n                            previous_call_id=str(observation.get("previous_call_id", "")),\n                            recovery=str(observation.get("recovery", "")),\n                        )\n                    if check.stop:\n                        return self._loop_limit_result(\n                            session.id,\n                            task,\n                            observations + [observation],\n                            f"tool loop limit reached after {observation.get('event')}",\n                            task_logger,\n                        )\n                else:\n                    execution_decision["session_id"] = session.id\n                    observation = await self._execute_tool(\n                        execution_decision,\n                        budget_plan=budget_plan,\n                        task_logger=task_logger,\n                    )\n                    self.loop_guard.record_result(check.call_id, observation)\n                event_id = self.task_store.append_event(\n                    session.id,\n                    role="tool",\n                    kind=str(observation.get("tool", "tool")),\n                    content=json.dumps(observation, separators=(",", ":")),\n                    metadata={"reason": str(decision.get("reason", ""))[:1000]},\n                )\n                observation = dict(observation)\n                observation["event_id"] = event_id\n                observations.append(observation)\n                continue\n\n            if action not in _FINAL_ACTIONS:\n                raise TaskOrchestratorError(f"unknown final action {action!r}")\n\n'''
text = regex_once(
    text,
    r'            # Small local models occasionally emit a tool with the final-action.*?            if action not in _FINAL_ACTIONS:\n                raise TaskOrchestratorError\(f"unknown final action \{action!r\}"\)\n\n',
    run_block,
    "controller dispatch loop",
)

text = replace_once(
    text,
    '''            {\n                "name": "operation",\n                "description": (\n                    "Execute one read-only Nexus operation only after find_tool has "\n                    "returned that operation's schema in this session."\n                ),\n                "arguments": {\n                    "operation": "operation name from the latest find_tool result",\n                    "args": "object matching that schema",\n                },\n            },''',
    '''            {\n                "name": ACTION_CALL_TOOL,\n                "description": (\n                    "Invoke exactly one concrete read-only Nexus operation only after "\n                    "find_tool returned its schema in this persistent session."\n                ),\n                "arguments": {\n                    "tool": "tool identifier returned by find_tool",\n                    "arguments": "object matching the returned argument schema",\n                },\n            },''',
    "call_tool capability",
)

old_prompt = '''Choose exactly one next step.\nTool calls:\n{{"type":"tool","tool":"find_tool","goal":"plain-language capability needed","reason":"..."}}\n{{"type":"tool","tool":"operation","operation":"<operation returned by find_tool>","args":{{...}},"reason":"..."}}\n{{"type":"tool","tool":"memory_search","query":"...","limit":6,"reason":"..."}}\n{{"type":"tool","tool":"delegate","provider":"local_worker|codex_readonly|azure_openai","task":"bounded sub-problem","context":"optional","reason":"..."}}\n{{"type":"tool","tool":"command","command":"<shell command>","reason":"..."}}\n{{"type":"tool","tool":"python_run","script":"<repo-relative .py>","argv":["--version"],"reason":"..."}}\nFinal action:\n{{"type":"action","action":"new_experiment|resume_experiment|answer|missing_capability|clarification","experiment_id":null,"message":"...","reason":"..."}}'''
new_prompt = '''Choose exactly one next step using ONE unambiguous `action` field.\nTool discovery/execution:\n{{"action":"find_tool","goal":"plain-language capability needed","reason":"..."}}\n{{"action":"call_tool","tool":"<tool returned by find_tool>","arguments":{{...}},"reason":"..."}}\nController-native capabilities:\n{{"action":"memory_search","query":"...","limit":6,"reason":"..."}}\n{{"action":"delegate","provider":"local_worker|codex_readonly|azure_openai","task":"bounded sub-problem","context":"optional","reason":"..."}}\n{{"action":"command","command":"<shell command>","reason":"..."}}\n{{"action":"python_run","script":"<repo-relative .py>","argv":["--version"],"reason":"..."}}\nFinal action:\n{{"action":"new_experiment|resume_experiment|answer|missing_capability|clarification","experiment_id":null,"message":"...","reason":"..."}}'''
text = replace_once(text, old_prompt, new_prompt, "prompt action examples")

text = replace_once(
    text,
    '- Use find_tool when you need an OperationsRegistry capability that has not already been resolved in this session; do not guess operation names.',
    '- Use find_tool when you need an OperationsRegistry capability that has not already been resolved in this session; then invoke only the returned identifier with call_tool. Never guess concrete tool names.',
    "find/call rule",
)

helper = '''    def _action_to_tool_decision(self, decision: dict[str, Any]) -> dict[str, Any]:\n        """Translate the action-only controller protocol to internal tool dispatch.\n\n        The outer LLM never needs the legacy type/tool/operation envelope. Native\n        controller capabilities remain internal actions; OperationsRegistry calls\n        always use find_tool -> call_tool.\n        """\n        action = str(decision.get("action", "")).strip().lower()\n        normalized = dict(decision)\n        if action == ACTION_CALL_TOOL:\n            normalized["tool"] = "operation"\n            normalized["operation"] = str(decision.get("tool", "")).strip()\n            normalized["args"] = decision.get("arguments") or {}\n            return normalized\n        normalized["tool"] = action\n        return normalized\n\n'''
text = regex_once(
    text,
    r'    def _normalize_tool_decision\(self, decision: dict\[str, Any\]\) -> dict\[str, Any\]:.*?\n    def _tool_identity',
    helper + '    def _tool_identity',
    "replace normalization helper",
)

parse_method = '''    @staticmethod\n    def _parse(raw: str) -> dict[str, Any]:\n        try:\n            data = extract_json(raw)\n        except LlamaCppClientError as exc:\n            raise TaskOrchestratorError(str(exc)) from exc\n        if not isinstance(data, dict):\n            raise TaskOrchestratorError("orchestrator response must be a JSON object")\n\n        # Temporary read compatibility for persisted tests/sessions created before\n        # issue #111. The controller schema and prompt no longer expose this shape.\n        response_type = str(data.get("type", "")).strip().lower()\n        if response_type in {"tool", "action"}:\n            legacy = dict(data)\n            legacy.pop("type", None)\n            if response_type == "tool":\n                legacy_tool = str(data.get("tool", "")).strip()\n                if not legacy_tool:\n                    raise TaskOrchestratorError("tool response requires tool")\n                if legacy_tool == "operation":\n                    legacy["action"] = ACTION_CALL_TOOL\n                    legacy["tool"] = str(data.get("operation", "")).strip()\n                    legacy["arguments"] = data.get("args") or {}\n                    legacy.pop("operation", None)\n                    legacy.pop("args", None)\n                elif legacy_tool in _ORCHESTRATOR_TOOLS:\n                    legacy["action"] = legacy_tool\n                else:\n                    legacy["action"] = ACTION_CALL_TOOL\n                    legacy["tool"] = legacy_tool\n                    legacy["arguments"] = data.get("args") or {}\n                    legacy.pop("args", None)\n            data = legacy\n        elif response_type:\n            raise TaskOrchestratorError(f"unknown response type {response_type!r}")\n\n        action = str(data.get("action", "")).strip().lower()\n        if not action:\n            raise TaskOrchestratorError("action response requires action")\n        if action == TOOL_FIND_TOOL and not str(data.get("goal", "")).strip():\n            raise TaskOrchestratorError("find_tool requires goal")\n        if action == ACTION_CALL_TOOL:\n            if not str(data.get("tool", "")).strip():\n                raise TaskOrchestratorError("call_tool requires tool")\n            if "arguments" not in data:\n                raise TaskOrchestratorError("call_tool requires arguments")\n            if not isinstance(data.get("arguments"), dict):\n                raise TaskOrchestratorError("call_tool arguments must be an object")\n        if action not in _ORCHESTRATOR_TOOLS and action not in _FINAL_ACTIONS:\n            raise TaskOrchestratorError(f"unknown final action {action!r}")\n        data["action"] = action\n        return data\n\n'''
text = regex_once(
    text,
    r'    @staticmethod\n    def _parse\(raw: str\) -> dict\[str, Any\]:.*?\n    async def _execute_tool',
    parse_method + '    async def _execute_tool',
    "action-only parser",
)

text = replace_once(
    text,
    '''                "operation": selected,\n                "reason": reason,''',
    '''                "resolved_tool": operation,\n                "arguments_schema": selected.get("arguments", []),\n                "operation": selected,\n                "reason": reason,''',
    "find_tool result schema",
)

ORCH.write_text(text, encoding="utf-8")

# Update the two prompt-specific assertions that intentionally describe the old protocol.
t = TEST_MAIN.read_text(encoding="utf-8")
t = replace_once(t, "self.assertIn('\\\"tool\\\":\\\"find_tool\\\"', client.prompts[0])", "self.assertIn('\\\"action\\\":\\\"find_tool\\\"', client.prompts[0])\n        self.assertNotIn('\\\"type\\\":\\\"tool\\\"', client.prompts[0])", "main prompt assertion")
TEST_MAIN.write_text(t, encoding="utf-8")

t = TEST_COMMAND.read_text(encoding="utf-8")
t = replace_once(t, "self.assertIn('{\\\"type\\\":\\\"tool\\\",\\\"tool\\\":\\\"command\\\"', prompt)", "self.assertIn('{\\\"action\\\":\\\"command\\\"', prompt)\n            self.assertNotIn('{\\\"type\\\":\\\"tool\\\"', prompt)", "command prompt assertion")
TEST_COMMAND.write_text(t, encoding="utf-8")

NEW_TEST.write_text(r'''from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.task_orchestrator import ACTION_ANSWER, NexusTaskOrchestrator, TaskOrchestratorError


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.response_formats = []

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        self.prompts.append(prompt)
        self.response_formats.append(response_format)
        return self.responses.pop(0)


class _Spec:
    name = "git_status"
    description = "Git status of the repository"
    tags = ["git", "status", "repository", "changes"]
    arguments = []

    def to_dict(self):
        return {
            "operation": self.name,
            "description": self.description,
            "arguments": [],
            "tags": self.tags,
        }


class _Registry:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.calls = []

    def catalog(self):
        return [_Spec()]

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, args, caller))
        return SimpleNamespace(to_dict=lambda: {
            "operation": operation,
            "success": True,
            "output_lines": ["clean"],
            "duration_ms": 1,
            "exit_code": 0,
            "error_summary": None,
        })


class _Delegation:
    def catalog(self):
        return []


class Issue111ControllerProtocolTests(unittest.TestCase):
    def make_orchestrator(self, tmp, responses):
        registry = _Registry(str(Path(tmp) / "state.db"), tmp)
        orchestrator = NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=MagicMock(),
            llama_client=_Client(responses),
            delegation_service=_Delegation(),
        )
        return orchestrator, registry, orchestrator.llama

    def test_schema_and_prompt_use_action_only_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _registry, client = self.make_orchestrator(
                tmp, ['{"action":"answer","message":"done","reason":"done"}']
            )
            try:
                result = orchestrator.run("answer")
            finally:
                orchestrator.close()
        self.assertEqual(result.action, ACTION_ANSWER)
        schema = client.response_formats[0]["schema"]
        self.assertEqual(schema["required"], ["action"])
        self.assertNotIn("type", schema["properties"])
        self.assertIn("call_tool", schema["properties"]["action"]["enum"])
        self.assertIn('{"action":"find_tool"', client.prompts[0])
        self.assertIn('{"action":"call_tool"', client.prompts[0])

    def test_missing_find_tool_goal_is_corrected_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _registry, client = self.make_orchestrator(tmp, [
                '{"action":"find_tool","reason":"need tool"}',
                '{"action":"answer","message":"corrected","reason":"fallback"}',
            ])
            try:
                result = orchestrator.run("inspect")
            finally:
                orchestrator.close()
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertIn("find_tool requires goal", client.prompts[1])

    def test_find_tool_then_call_tool_executes_resolved_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, registry, client = self.make_orchestrator(tmp, [
                '{"action":"find_tool","goal":"inspect repository git status","reason":"discover"}',
                '{"status":"found","operation":"git_status","reason":"direct match","related_tools":[]}',
                '{"action":"call_tool","tool":"git_status","arguments":{},"reason":"invoke resolved tool"}',
                '{"action":"answer","message":"clean","reason":"observed"}',
            ])
            try:
                result = orchestrator.run("inspect git status")
            finally:
                orchestrator.close()
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(registry.calls[0][0], "git_status")
        self.assertIn('"resolved_tool":"git_status"', client.prompts[2])
        self.assertIn('"grant_event_id"', client.prompts[3])

    def test_call_tool_requires_arguments(self):
        with self.assertRaisesRegex(TaskOrchestratorError, "call_tool requires arguments"):
            NexusTaskOrchestrator._parse('{"action":"call_tool","tool":"git_status"}')

    def test_unresolved_call_tool_is_rejected_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, registry, client = self.make_orchestrator(tmp, [
                '{"action":"call_tool","tool":"git_status","arguments":{},"reason":"guess"}',
                '{"action":"missing_capability","message":"not granted","reason":"operation_not_granted"}',
            ])
            try:
                result = orchestrator.run("inspect git status")
            finally:
                orchestrator.close()
        self.assertEqual(registry.calls, [])
        self.assertIn('"error":"operation_not_granted"', client.prompts[1])


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")

print("issue #111 patch applied")
