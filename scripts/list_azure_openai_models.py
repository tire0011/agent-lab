#!/usr/bin/env python3
"""Browse the Azure OpenAI models/deployments available to this environment.

Two views are printed:

1. Data-plane: deployments actually reachable with the configured
   ``AZURE_OPENAI_API_KEY`` (via the resource's own REST API). This reflects
   exactly what ``NexusDelegationService`` can call.
2. Management-plane (optional): deployments visible via ``az`` CLI, if the
   caller is logged in and authorized. Useful cross-check / admin view.

Usage:
    python scripts/list_azure_openai_models.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Reuse the same env-loading behaviour as test_azure_openai.py so this script
# works standalone on the VM (root-only /etc/agent-lab/agent.env) as well as
# locally where env vars are already exported.
_AGENT_ENV_FILE = Path("/etc/agent-lab/agent.env")


def _parse_env_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _load_agent_env(path: Path = _AGENT_ENV_FILE) -> None:
    if not path.exists():
        return

    text: str | None = None
    try:
        text = path.read_text()
    except PermissionError:
        try:
            proc = subprocess.run(
                ["sudo", "-n", "cat", str(path)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                text = proc.stdout
        except (OSError, subprocess.SubprocessError):
            text = None
    except OSError:
        text = None

    if not text:
        return

    for key, value in _parse_env_lines(text).items():
        os.environ.setdefault(key, value)


# Meters that aren't plain per-token chat/completion pricing (fine-tuning,
# batch, realtime/audio, TTS, transcription, cached-input variants, etc.).
# We only want the standard pay-as-you-go input/output token rates.
_PRICE_EXCLUDE_KEYWORDS = (
    "batch", "realtime", " rt ", "-rt-", "aud", "tts", "transcribe", "tcrb",
    " ft ", "-ft-", "grader", "grdr", "cchd", "cached", "fine-tun", "finetun",
    "dev-ft", "dev ft", "training", "hosting", "rft",
)


def _norm_token(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _fetch_region_prices(region: str) -> list[dict]:
    """Fetch all Azure OpenAI consumption pricing for ``region`` from the
    public Azure Retail Prices API (no auth required). Paginates as needed.
    """
    items: list[dict] = []
    url = (
        "https://prices.azure.com/api/retail/prices"
        f"?$filter=serviceName eq 'Foundry Models' and armRegionName eq '{region}'"
        " and contains(productName,'Azure OpenAI')"
    )
    for _ in range(20):  # safety cap on pagination
        try:
            response = httpx.get(url, timeout=30.0)
        except httpx.HTTPError:
            break
        if response.status_code != 200:
            break
        data = response.json()
        items.extend(data.get("Items", []))
        url = data.get("NextPageLink")
        if not url:
            break
    return items


def _assign_prices_to_models(items: list[dict], model_names: list[str]) -> dict[str, list[dict]]:
    """Greedily assign each pricing meter to the most specific (longest
    name) catalog model it matches, avoiding e.g. 'gpt-5' swallowing
    'gpt-5-mini' meters.
    """
    import re

    ordered = sorted(set(model_names), key=len, reverse=True)
    patterns = [
        (name, re.compile(r"(?:^|-)" + re.escape(_norm_token(name)) + r"(?:-|$)"))
        for name in ordered
    ]

    assigned: dict[str, list[dict]] = {}
    for item in items:
        if item.get("unitOfMeasure") not in ("1K", "1M"):
            continue
        raw = item.get("meterName", "").lower()
        if any(kw in raw for kw in _PRICE_EXCLUDE_KEYWORDS):
            continue
        normalized = _norm_token(item.get("meterName", ""))
        for name, pattern in patterns:
            if pattern.search(normalized):
                assigned.setdefault(name, []).append(item)
                break
    return assigned


def _best_io_price_per_million(candidates: list[dict]) -> tuple[float | None, float | None]:
    """Pick the cheapest global (non-DataZone) input/output $-per-1M-token
    rate from ``candidates``. Returns (input_price, output_price); either
    may be None if not found. Embedding-style single-meter models will have
    only an "input" price (Azure doesn't split embeddings by output).
    """

    def per_million(item: dict) -> float:
        price = float(item.get("retailPrice", 0.0))
        return price * 1000.0 if item.get("unitOfMeasure") == "1K" else price

    def is_global(item: dict) -> bool:
        raw = item.get("meterName", "").lower()
        return "dz" not in raw and "data zone" not in raw and "dzone" not in raw

    global_candidates = [c for c in candidates if is_global(c)] or candidates

    inputs = [
        c for c in global_candidates
        if "inp" in c.get("meterName", "").lower()
    ]
    outputs = [
        c for c in global_candidates
        if "outp" in c.get("meterName", "").lower()
    ]

    input_price = min((per_million(c) for c in inputs), default=None)
    output_price = min((per_million(c) for c in outputs), default=None)

    # Single-meter models (e.g. embeddings) have neither "inp" nor "outp" in
    # the meter name; treat the sole price as the (only) input price.
    if input_price is None and output_price is None and global_candidates:
        input_price = min(per_million(c) for c in global_candidates)

    return input_price, output_price


def _format_price(value: float | None) -> str:
    return f"${value:.4f}/1M tok" if value is not None else "n/a"


def _print_data_plane_deployments(endpoint: str, api_key: str) -> None:
    print("== Data-plane deployments (usable with current API key) ==")
    url = f"{endpoint}/openai/deployments?api-version=2023-03-15-preview"
    try:
        response = httpx.get(
            url, headers={"api-key": api_key}, timeout=30.0
        )
    except httpx.HTTPError as exc:
        print(f"  request failed: {type(exc).__name__}: {exc}")
        return

    if response.status_code != 200:
        detail = response.text.replace(api_key, "[REDACTED]")
        print(f"  HTTP {response.status_code}: {detail}")
        return

    data = response.json().get("data", [])
    if not data:
        print("  (no deployments returned)")
        return

    for item in data:
        model = item.get("model", "?")
        deployment_id = item.get("id", "?")
        status = item.get("status", "?")
        print(f"  - deployment={deployment_id!r} model={model!r} status={status}")


def _account_name_from_endpoint(endpoint: str) -> str:
    """Derive the Cognitive Services account name from its endpoint host.

    e.g. https://openai-tire0011.openai.azure.com -> openai-tire0011
    """
    host = endpoint.split("//", 1)[-1]
    return host.split(".", 1)[0]


def _discover_account(az_path: str, account_hint: str) -> tuple[str, str]:
    """Resolve the actual (correctly-cased) account name and its resource
    group via az CLI (best effort). Matching is case-insensitive because
    Azure hostnames are always lowercased, but resource names may not be.
    """
    try:
        proc = subprocess.run(
            [
                az_path,
                "cognitiveservices",
                "account",
                "list",
                "--query",
                "[].{name:name, resourceGroup:resourceGroup}",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "", ""
    if proc.returncode != 0:
        return "", ""
    try:
        accounts = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "", ""
    for entry in accounts:
        name = str(entry.get("name", ""))
        if name.lower() == account_hint.lower():
            return name, str(entry.get("resourceGroup", ""))
    return "", ""


def _resolve_account(az_path: str, endpoint: str) -> tuple[str, str]:
    """Resolve (account_name, resource_group), honoring env overrides."""
    account_env = os.environ.get("AZURE_OPENAI_ACCOUNT_NAME")
    resource_group_env = os.environ.get("AZURE_OPENAI_RESOURCE_GROUP")
    account_hint = account_env or _account_name_from_endpoint(endpoint)

    if account_env and resource_group_env:
        return account_env, resource_group_env

    discovered_name, discovered_rg = _discover_account(az_path, account_hint)
    account = account_env or discovered_name or account_hint
    resource_group = resource_group_env or discovered_rg
    return account, resource_group


def _run_az_json(az_path: str, args: list[str]) -> list | None:
    try:
        proc = subprocess.run(
            [az_path, *args, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  az invocation failed: {exc}")
        return None
    if proc.returncode != 0:
        print(f"  az error: {proc.stderr.strip()}")
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("  could not parse az output")
        return None


def _resolve_region(az_path: str, account: str, resource_group: str) -> str:
    result = _run_az_json_quiet(
        az_path,
        [
            "cognitiveservices",
            "account",
            "show",
            "--name",
            account,
            "--resource-group",
            resource_group,
            "--query",
            "location",
        ],
    )
    return str(result) if isinstance(result, str) else ""


def _run_az_json_quiet(az_path: str, args: list[str]):
    """Like _run_az_json but silent on failure (used for optional lookups)."""
    try:
        proc = subprocess.run(
            [az_path, *args, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _print_management_plane_deployments(
    deployments: list | None, capabilities: dict, prices: dict
) -> None:
    print("\n== Management-plane deployments (currently provisioned) ==")
    if deployments is None:
        print("  (lookup failed)")
        return
    if not deployments:
        print("  (none)")
        return

    for dep in deployments:
        name = dep.get("name", "?")
        props = dep.get("properties", {})
        model = props.get("model", {})
        model_name = model.get("name")
        model_version = model.get("version")
        caps = capabilities.get((model_name, model_version), {})
        ctx = caps.get("maxContextToken", "?")
        out_tok = caps.get("maxOutputToken", "?")
        in_price, out_price = _best_io_price_per_million(prices.get(model_name, []))
        print(
            f"  - deployment={name!r} model={model_name!r} version={model_version!r}\n"
            f"      context={ctx} tokens, max_output={out_tok} tokens\n"
            f"      price: input={_format_price(in_price)}, output={_format_price(out_price)}"
        )


def _print_available_catalog(
    models: list | None, deployments: list | None, capabilities: dict, prices: dict
) -> None:
    print(
        "\n== Model catalog available to deploy on this resource "
        "(not yet provisioned) =="
    )
    if models is None:
        print("  (lookup failed)")
        return
    if not models:
        print("  (none)")
        return

    deployed_pairs = {
        (dep.get("properties", {}).get("model", {}).get("name"),
         dep.get("properties", {}).get("model", {}).get("version"))
        for dep in (deployments or [])
    }

    grouped: dict[str, list[str]] = {}
    for m in models:
        name = m.get("name", "?")
        version = m.get("version", "?")
        if (name, version) in deployed_pairs:
            continue
        grouped.setdefault(name, []).append(version)

    if not grouped:
        print("  (all catalog models are already deployed)")
        return

    for name in sorted(grouped):
        versions = sorted(set(grouped[name]))
        # Use the newest version's capabilities as representative.
        caps = capabilities.get((name, versions[-1]), {})
        ctx = caps.get("maxContextToken", "?")
        out_tok = caps.get("maxOutputToken", "?")
        in_price, out_price = _best_io_price_per_million(prices.get(name, []))
        print(
            f"  - {name}: versions {', '.join(versions)}\n"
            f"      context={ctx} tokens, max_output={out_tok} tokens\n"
            f"      price: input={_format_price(in_price)}, output={_format_price(out_price)}"
        )


def _build_capabilities(models: list) -> dict:
    caps: dict[tuple, dict] = {}
    for m in models:
        capabilities = m.get("capabilities") or {}
        max_ctx = capabilities.get("maxContextToken")
        max_out = capabilities.get("maxOutputToken")
        caps[(m.get("name"), m.get("version"))] = {
            "maxContextToken": max_ctx,
            "maxOutputToken": max_out,
        }
    return caps


def main() -> int:
    _load_agent_env()

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")

    print(f"endpoint: {endpoint or '<not configured>'}")

    if not endpoint or not api_key:
        print("AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not configured")
        return 1

    _print_data_plane_deployments(endpoint, api_key)

    az_path = shutil.which("az")
    if not az_path:
        print("\naz CLI not found; skipping management-plane views")
        return 0

    account, resource_group = _resolve_account(az_path, endpoint)
    if not resource_group:
        print(
            f"\ncould not resolve resource group for account {account!r} "
            "(not logged in to az, or insufficient permissions); "
            "set AZURE_OPENAI_RESOURCE_GROUP to override (skipping)"
        )
        return 0

    deployments = _run_az_json(
        az_path,
        [
            "cognitiveservices", "account", "deployment", "list",
            "--name", account, "--resource-group", resource_group,
        ],
    )
    models = _run_az_json(
        az_path,
        [
            "cognitiveservices", "account", "list-models",
            "--name", account, "--resource-group", resource_group,
        ],
    )
    capabilities = _build_capabilities(models or [])

    region = _resolve_region(az_path, account, resource_group)
    prices: dict[str, list[dict]] = {}
    if region:
        model_names = [m.get("name") for m in (models or []) if m.get("name")]
        price_items = _fetch_region_prices(region)
        prices = _assign_prices_to_models(price_items, model_names)
    else:
        print(
            "\ncould not resolve region for pricing lookup; "
            "showing token limits only, no $ pricing"
        )

    _print_management_plane_deployments(deployments, capabilities, prices)
    _print_available_catalog(models, deployments, capabilities, prices)
    print(
        "\nNote: pricing is a best-effort match against the public Azure "
        "Retail Prices API (pay-as-you-go, global standard SKU) and may not "
        "reflect negotiated/enterprise rates. Verify at "
        "https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/"
        " before relying on it for cost decisions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
