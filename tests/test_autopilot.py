import json
from unittest.mock import patch

import pytest

from pmoagent.autopilot import main

ENDPOINT = "https://example.services.ai.azure.com/api/projects/pmo"


def dry_run(capsys, *args: str) -> tuple[str, list[dict]]:
    with patch("pmoagent.autopilot.AzureCliCredential") as credential:
        main(list(args))
        credential.assert_not_called()
    out = capsys.readouterr().out
    bodies, buffer = [], []
    for line in out.splitlines():
        if line.startswith("[dry-run]"):
            if buffer:
                bodies.append(json.loads("\n".join(buffer)))
            buffer = []
        else:
            buffer.append(line)
    if buffer:
        bodies.append(json.loads("\n".join(buffer)))
    return out, bodies


def test_create_dry_run_builds_activity_autopilot_agent(capsys):
    out, (version, patch_body) = dry_run(
        capsys, "create", "--project-endpoint", ENDPOINT, "--image", "x.azurecr.io/pmo:1",
        "--model", "claude", "--ado-organization", "contoso", "--storage-account", "pmostore",
    )  # fmt: skip
    assert f"POST {ENDPOINT}/agents/pmo-agent/versions?api-version=2025-11-15-preview" in out
    definition = version["definition"]
    assert definition["container_protocol_versions"] == [
        {"protocol": "activity_protocol", "version": "2.0.0"}
    ]
    assert version["digital_worker_type"] == "m365"
    env = definition["environment_variables"]
    assert env["PMO_ENVIRONMENT"] == "azure" and env["PMO_STORAGE_ACCOUNT_NAME"] == "pmostore"
    assert env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "false"
    assert not any("SECRET" in key or "KEY" in key for key in env)
    schemes = patch_body["agent_endpoint"]["authorization_schemes"]
    assert schemes == [{"type": "BotServiceRbac"}]


def test_publish_dry_run_is_group_autopilot_with_mcp_scopes(capsys):
    _, (body,) = dry_run(
        capsys, "publish", "--project-endpoint", ENDPOINT, "--blueprint-client-id", "bp",
        "--developer", "Contoso", "--website", "https://contoso.example",
        "--privacy-url", "https://contoso.example/privacy", "--terms-url", "https://contoso.example/terms",
    )  # fmt: skip
    assert body["publishAsAutopilot"] and body["useAgenticUserTemplate"]
    assert body["publishScope"] == "Tenant" and body["canRespondWithoutMention"] is False
    assert body["agenticUserTemplate"]["AgentIdentityBlueprintId"] == "bp"
    scopes = {s["resourceAppId"]: s["scopes"] for s in body["optionalPermissionScopes"]}
    assert scopes["2a72489c-aab2-4b65-b93a-a91edccf33b8"] == ["Ado.Mcp.Tools"]
    assert "McpServers.Mail.All" not in scopes["ea9ffc3e-8a23-4a7d-836d-234d7c7565c1"]


def test_grant_dry_run_scopes_blob_access_to_container(capsys):
    out, _ = dry_run(
        capsys, "grant", "--runtime-client-id", "rid", "--project-resource-id", "/proj",
        "--storage-account-id", "/sa",
    )  # fmt: skip
    assert "--role Foundry User --scope /proj" in out
    assert "/sa/blobServices/default/containers/pmo-conversations" in out


@pytest.mark.parametrize(
    "args",
    [
        ["create", "--project-endpoint", "https://evil.example/api/projects/p", "--image", "i",
         "--model", "m", "--ado-organization", "o", "--storage-account", "s"],
        ["publish", "--project-endpoint", ENDPOINT, "--blueprint-client-id", "bp", "--developer", "d",
         "--website", "http://insecure.example", "--privacy-url", "https://x/p", "--terms-url", "https://x/t"],
    ],
)  # fmt: skip
def test_invalid_inputs_are_rejected(args):
    with pytest.raises(SystemExit):
        main(args)
