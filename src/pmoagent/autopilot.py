"""Create, authorize, and publish the PMO agent as a Foundry autopilot blueprint.

Every command is a dry run unless --apply is given: it prints the exact requests or
commands without acquiring tokens or changing anything.
"""

import argparse
import json
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from azure.identity import AzureCliCredential

API_VERSION = "2025-11-15-preview"
FOUNDRY_USER_ROLE = "Foundry User"
BLOB_CONTRIBUTOR_ROLE = "Storage Blob Data Contributor"
PERMISSION_SCOPES = [
    {
        "resourceAppId": "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1",
        "scopes": ["McpServers.Teams.All", "McpServers.Calendar.All"],
    },
    {"resourceAppId": "2a72489c-aab2-4b65-b93a-a91edccf33b8", "scopes": ["Ado.Mcp.Tools"]},
]


def project_endpoint(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith(".services.ai.azure.com"):
        raise argparse.ArgumentTypeError("Expected a Foundry project HTTPS endpoint")
    return value.rstrip("/")


def runtime_environment(args: argparse.Namespace, runtime_client_id: str | None) -> dict[str, str]:
    env = {
        "PYTHONNOUSERSITE": "1",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
        "PMO_ENVIRONMENT": "azure",
        "PMO_ANTHROPIC_MODEL": args.model,
        "PMO_ADO_ORGANIZATION": args.ado_organization,
        "PMO_STORAGE_ACCOUNT_NAME": args.storage_account,
    }
    if args.ado_project:
        env["PMO_ADO_PROJECT"] = args.ado_project
    if runtime_client_id:
        env["PMO_RUNTIME_CLIENT_ID"] = runtime_client_id
    return env


def version_body(args: argparse.Namespace, runtime_client_id: str | None) -> dict[str, Any]:
    return {
        "definition": {
            "kind": "hosted",
            "image": args.image,
            "cpu": "1",
            "memory": "2Gi",
            "environment_variables": runtime_environment(args, runtime_client_id),
            "container_protocol_versions": [{"protocol": "activity_protocol", "version": "2.0.0"}],
        },
        "metadata": {"enableVnextExperience": "true"},
        "description": "PMO agent (Claude Agent SDK) autopilot blueprint.",
        "agent_endpoint": endpoint_body(authorized=False),
        "digital_worker_type": "m365",
    }


def endpoint_body(authorized: bool) -> dict[str, Any]:
    endpoint: dict[str, Any] = {
        "protocols": ["activity"],
        "protocol_configuration": {"activity": {"enable_m365_public_endpoint": True}},
    }
    if authorized:
        endpoint["authorization_schemes"] = [{"type": "BotServiceRbac"}]
    return endpoint


def publish_body(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "agentDisplayName": args.display_name,
        "publishAsAutopilot": True,
        "publishScope": "Tenant",
        "appVersion": args.app_version,
        # Group autopilot: answer only when @mentioned.
        "canRespondWithoutMention": False,
        "shortDescription": "Azure DevOpsとMicrosoft 365から根拠付きの報告ドラフトを作成します。",
        "fullDescription": (
            "チームのチャットで@メンションされると、読み取り専用ツールで情報を集めて"
            "プロジェクト報告ドラフトを作成します。承認・差戻しは依頼者だけが行えます。"
        ),
        "developerName": args.developer,
        "developerWebsiteUrl": args.website,
        "privacyUrl": args.privacy_url,
        "termsOfUseUrl": args.terms_url,
        "optionalPermissionScopes": PERMISSION_SCOPES,
        "useAgenticUserTemplate": True,
        "agenticUserTemplate": {
            "Id": "digitalWorkerTemplate",
            "File": "agenticUserTemplateManifest.json",
            "SchemaVersion": "0.1.0-preview",
            "AgentIdentityBlueprintId": args.blueprint_client_id,
            "CommunicationProtocol": "activityProtocol",
        },
    }


class Foundry:
    def __init__(self, endpoint: str, apply: bool) -> None:
        self.endpoint = endpoint
        self.apply = apply
        self.client: httpx.Client | None = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.endpoint}{path}?api-version={API_VERSION}"
        if not self.apply:
            print(f"[dry-run] {method} {url}")
            if body is not None:
                print(json.dumps(body, ensure_ascii=False, indent=2))
            return {}
        if self.client is None:
            token = AzureCliCredential().get_token("https://ai.azure.com/.default").token
            self.client = httpx.Client(
                timeout=60,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Foundry-Features": "DigitalWorker=V1Preview",
                },
            )
        response = self.client.request(method, url, json=body)
        if response.is_error:
            raise SystemExit(f"{method} {path} failed: HTTP {response.status_code} {response.text}")
        data: dict[str, Any] = response.json() if response.content else {}
        return data

    def create_version(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        created = self.request("POST", f"/agents/{name}/versions", body)
        if not self.apply:
            return {}
        version = created["version"]
        for _ in range(60):
            if created.get("status") in {"active", "failed"}:
                break
            time.sleep(10)
            created = self.request("GET", f"/agents/{name}/versions/{version}")
        if created.get("status") != "active":
            raise SystemExit(
                f"Agent version {version} is {created.get('status')}: {created.get('error')}"
            )
        return created


def create(args: argparse.Namespace) -> None:
    foundry = Foundry(args.project_endpoint, args.apply)
    created = foundry.create_version(args.name, version_body(args, args.runtime_client_id))
    runtime_client_id = args.runtime_client_id
    if args.apply and not runtime_client_id:
        # The default instance identity exists only after the first version is created.
        runtime_client_id = created["instance_identity"]["client_id"]
        created = foundry.create_version(args.name, version_body(args, runtime_client_id))
    foundry.request(
        "PATCH", f"/agents/{args.name}", {"agent_endpoint": endpoint_body(authorized=True)}
    )
    if args.apply:
        print(
            json.dumps(
                {
                    "version": created["version"],
                    "blueprint_client_id": created["blueprint"]["client_id"],
                    "runtime_client_id": runtime_client_id,
                },
                indent=2,
            )
        )


def grant(args: argparse.Namespace) -> None:
    commands = [
        [
            "az", "role", "assignment", "create", "--assignee", args.runtime_client_id,
            "--role", FOUNDRY_USER_ROLE, "--scope", args.project_resource_id,
        ],
        [
            "az", "role", "assignment", "create", "--assignee", args.runtime_client_id,
            "--role", BLOB_CONTRIBUTOR_ROLE,
            "--scope", f"{args.storage_account_id}/blobServices/default/containers/{args.container}",
        ],
    ]  # fmt: skip
    for command in commands:
        if not args.apply:
            print("[dry-run] " + " ".join(command))
            continue
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode and "RoleAssignmentExists" not in result.stderr:
            raise SystemExit(result.stderr)
        print(f"granted: {command[7]}")


def publish(args: argparse.Namespace) -> None:
    for url in (args.website, args.privacy_url, args.terms_url):
        if urlparse(url).scheme != "https":
            raise SystemExit(f"HTTPS URL required: {url}")
    Foundry(args.project_endpoint, args.apply).request(
        "POST", f"/agents/{args.name}/microsoft365/publish", publish_body(args)
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--apply", action="store_true", help="Execute instead of printing")

    foundry = argparse.ArgumentParser(add_help=False)
    foundry.add_argument("--project-endpoint", type=project_endpoint, required=True)
    foundry.add_argument("--name", default="pmo-agent")

    create_cmd = commands.add_parser("create", parents=[common, foundry])
    create_cmd.add_argument(
        "--image", required=True, help="ACR image, for example x.azurecr.io/pmo-agent:1"
    )
    create_cmd.add_argument("--model", required=True, help="Claude deployment name")
    create_cmd.add_argument("--ado-organization", required=True)
    create_cmd.add_argument("--ado-project")
    create_cmd.add_argument("--storage-account", required=True)
    create_cmd.add_argument("--runtime-client-id")
    create_cmd.set_defaults(func=create)

    grant_cmd = commands.add_parser("grant", parents=[common])
    grant_cmd.add_argument("--runtime-client-id", required=True)
    grant_cmd.add_argument("--project-resource-id", required=True)
    grant_cmd.add_argument("--storage-account-id", required=True)
    grant_cmd.add_argument("--container", default="pmo-conversations")
    grant_cmd.set_defaults(func=grant)

    publish_cmd = commands.add_parser("publish", parents=[common, foundry])
    publish_cmd.add_argument("--blueprint-client-id", required=True)
    publish_cmd.add_argument("--app-version", default="1.0.0")
    publish_cmd.add_argument("--display-name", default="PMO Agent")
    publish_cmd.add_argument("--developer", required=True)
    publish_cmd.add_argument("--website", required=True)
    publish_cmd.add_argument("--privacy-url", required=True)
    publish_cmd.add_argument("--terms-url", required=True)
    publish_cmd.set_defaults(func=publish)
    return root


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
