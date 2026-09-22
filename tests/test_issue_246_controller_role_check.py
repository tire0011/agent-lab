from scripts.check_issue_246_controller_role import verdict


def test_initial_rejects_reasoning_as_find_tool_capability():
    ok, reason = verdict(
        "initial",
        {
            "action": "find_tool",
            "goal": "Identify the root cause of GitHub issue #238 and propose a solution",
        },
    )
    assert not ok
    assert "reasoning/development" in reason


def test_initial_accepts_concrete_issue_lookup():
    ok, reason = verdict(
        "initial",
        {
            "action": "find_tool",
            "goal": "Fetch the contents of GitHub issue #238",
        },
    )
    assert ok, reason


def test_after_read_accepts_command():
    ok, reason = verdict(
        "after-read",
        {
            "action": "command",
            "command": "sed -n '1,220p' modules/nexus_memory.py",
        },
    )
    assert ok, reason


def test_after_read_accepts_python_run():
    ok, reason = verdict(
        "after-read",
        {
            "action": "python_run",
            "script": "scripts/check_issue_196_controller_progress.py",
        },
    )
    assert ok, reason


def test_after_read_rejects_repeated_issue_read():
    ok, reason = verdict(
        "after-read",
        {
            "action": "call_tool",
            "tool": "github_issue",
            "arguments": {"issue_number": 238},
        },
    )
    assert not ok
    assert "expected command/python_run" in reason


def test_after_read_rejects_false_completion():
    ok, reason = verdict(
        "after-read",
        {
            "action": "answer",
            "message": "Issue #238 is resolved.",
        },
    )
    assert not ok
    assert "expected command/python_run" in reason


def test_after_read_rejects_missing_capability():
    ok, reason = verdict(
        "after-read",
        {
            "action": "missing_capability",
            "information_gap": "resolve the root cause and propose a solution",
        },
    )
    assert not ok
    assert "expected command/python_run" in reason
