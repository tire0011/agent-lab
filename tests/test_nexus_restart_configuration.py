from pathlib import Path
import re


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "nexus.ps1"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    text = _script()
    start = text.index(f"function {name}")
    next_function = text.find("\nfunction ", start + 1)
    if next_function < 0:
        next_function = text.find("\nswitch (", start + 1)
    return text[start:next_function]


def test_restart_validates_full_lifecycle_configuration_before_systemctl():
    body = _function_body("Restart-Nexus")

    validation = body.index("Assert-NexusConfiguration")
    first_service_change = body.index("systemctl")

    assert validation < first_service_change
    assert "Assert-AgentApiConfiguration" not in body


def test_configuration_requires_api_key_repository_and_github_token_alternative():
    body = _function_body("Assert-NexusConfiguration")

    assert "Test-AgentEnvironmentValue 'AGENT_API_KEY'" in body
    assert "Test-AgentEnvironmentValue 'GITHUB_REPOSITORY'" in body
    assert "Test-AgentEnvironmentValue 'GITHUB_TOKEN|GH_TOKEN'" in body


def test_missing_repository_and_token_errors_name_variable_and_env_file():
    body = _function_body("Assert-NexusConfiguration")

    assert re.search(
        r'GITHUB_REPOSITORY is missing or empty in \$AgentEnvironmentFile', body
    )
    assert re.search(
        r'GITHUB_TOKEN or GH_TOKEN is missing or empty in \$AgentEnvironmentFile', body
    )


def test_configuration_validation_is_boolean_only_and_does_not_read_secret_values():
    helper = _function_body("Test-AgentEnvironmentValue")
    assertion = _function_body("Assert-NexusConfiguration")

    # grep -q emits no matching line; validation consumes only its exit code.
    assert "grep -Eq" in helper
    assert "$LASTEXITCODE -eq 0" in helper

    forbidden_value_readers = ("Get-Content", "cat ", "Select-String")
    combined = helper + assertion
    for command in forbidden_value_readers:
        assert command not in combined

    # Errors may name secret variables, but no captured/matched value is rendered.
    assert "$Matches" not in combined
    assert "Write-Output" not in combined


def test_empty_assignments_do_not_match_the_presence_pattern():
    helper = _function_body("Test-AgentEnvironmentValue")

    # The grep expression requires a non-whitespace character after '='.
    assert "=[[:space:]]*[^[:space:]].*$" in helper
