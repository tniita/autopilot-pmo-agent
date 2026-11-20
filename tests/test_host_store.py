import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from azure.identity.aio import DefaultAzureCredential
from microsoft_agents.activity import Activity, ChannelAccount, ConversationAccount

from pmoagent.agent import ReportGenerator
from pmoagent.gpt import GptReportGenerator
from pmoagent.host import (
    build_app,
    conversation_key,
    handle_message,
    sdk_configuration,
    teams_location,
)
from pmoagent.store import Approval, ConversationStore, StoredDraft


def activity(**updates):
    data = {
        "type": "message",
        "id": "m1",
        "channel_id": "msteams",
        "text": "<at>PMO</at> 承認",
        "from_property": ChannelAccount(id="29:alice", aad_object_id="alice-id", name="Alice"),
        "recipient": ChannelAccount(
            id="28:pmo", name="PMO", role="agenticUser", agentic_app_id="inst", tenant_id="tid"
        ),
        "conversation": ConversationAccount(id="19:group", conversation_type="groupChat"),
        "entities": [
            {
                "type": "mention",
                "text": "<at>PMO</at>",
                "mentioned": {"id": "28:pmo", "name": "PMO"},
            }
        ],
    }
    data.update(updates)
    return Activity(**data)


def context_for(act):
    sent = []

    async def send(value):
        sent.append(value)

    return SimpleNamespace(activity=act, send_activity=send), sent


async def test_message_strips_mention_and_uses_verified_sender(settings):
    bot = SimpleNamespace(respond=AsyncMock(return_value="ok"))
    context, sent = context_for(activity())
    await handle_message(context, settings, bot, AsyncMock(return_value="token"))
    text, sender_id, sender_name, key, _ = bot.respond.call_args.args
    assert (text, sender_id, sender_name) == ("承認", "alice-id", "Alice")
    assert key == "inst:tid:19:group"
    assert sent[-1] == "ok"


@pytest.mark.parametrize(
    ("updates", "environment", "message"),
    [
        ({"from_property": ChannelAccount(id="29:x", name="X")}, "local", "送信者を確認できない"),
        ({"recipient": ChannelAccount(id="28:pmo", name="PMO")}, "azure", "Autopilot"),
        ({"text": "<at>PMO</at> " + "あ" * 2001}, "local", "2000文字"),
    ],
)
async def test_unverifiable_or_oversized_messages_are_rejected(
    settings, updates, environment, message
):
    settings.environment = environment
    bot = SimpleNamespace(respond=AsyncMock())
    context, sent = context_for(activity(**updates))
    await handle_message(context, settings, bot, AsyncMock())
    bot.respond.assert_not_awaited()
    assert message in sent[-1]


def test_conversation_key_isolates_instances():
    assert conversation_key(activity()) != conversation_key(
        activity(recipient=ChannelAccount(id="28:pmo", role="agenticUser", agentic_app_id="other"))
    )


def test_teams_location_for_channel_thread_and_group_chat():
    thread = activity(
        conversation=ConversationAccount(id="19:chan@thread.tacv2;messageid=123"),
        channel_data={
            "team": {"aadGroupId": "team-guid"},
            "channel": {"id": "19:chan@thread.tacv2"},
        },
    )
    assert teams_location(thread) == (
        "Teamsチャネル（team-id: team-guid, channel-id: 19:chan@thread.tacv2）"
    )
    chat = activity(conversation=ConversationAccount(id="19:group@thread.v2;messageid=9"))
    assert teams_location(chat) == "Teamsチャット（chat-id: 19:group@thread.v2）"


def test_sdk_configuration_uses_platform_blueprint_and_agentic_handler():
    config = sdk_configuration({"FOUNDRY_AGENT_BLUEPRINT_CLIENT_ID": "blueprint"})
    connection = config["CONNECTIONS"]["SERVICE_CONNECTION"]["SETTINGS"]
    assert connection["AUTHTYPE"] == "IdentityProxyManager"
    assert connection["CLIENTID"] == "blueprint"
    handler = config["AGENTAPPLICATION"]["USERAUTHORIZATION"]["HANDLERS"]["AGENTIC"]
    assert handler["TYPE"] == "AgenticUserAuthorization"


@pytest.mark.parametrize(
    "provider,backend", [("claude", ReportGenerator), ("gpt", GptReportGenerator)]
)
async def test_activity_endpoint_requires_jwt(settings, provider, backend, caplog):
    caplog.set_level("INFO", logger="pmoagent.host")
    settings.provider = provider
    settings.gpt_model = "test-gpt"
    with patch("pmoagent.host.PmoBot") as bot:
        app = build_app(settings, {"FOUNDRY_AGENT_BLUEPRINT_CLIENT_ID": str(uuid4())})
    assert isinstance(bot.call_args.args[2], backend)
    app.cleanup_ctx.clear()
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/readiness")).status == 200
        for path in ["/activity/messages", "/api/messages"]:
            for headers in [{}, {"Authorization": "private-test-header"}]:
                response = await client.post(
                    path, headers=headers, json={"type": "message", "text": "private-test-message"}
                )
                assert response.status == 401
    assert [record.message for record in caplog.records if record.name == "pmoagent.host"] == [
        f"activity_ingress path={path} mode=jwt authorization_present={present} status=401"
        for path in ["/activity/messages", "/api/messages"]
        for present in [False, True]
    ]


@pytest.mark.parametrize(
    ("environment", "hosting", "expected"),
    [("azure", "production", 202), ("local", "production", 401), ("azure", "", 401)],
)
async def test_foundry_gateway_ingress_only_when_hosted(settings, environment, hosting, expected):
    settings.environment = environment
    if environment == "azure":
        settings.storage_connection_string = None
        settings.storage_account_name = "pmodemo"
    environ = {"FOUNDRY_AGENT_BLUEPRINT_CLIENT_ID": str(uuid4())}
    if hosting:
        environ["FOUNDRY_HOSTING_ENVIRONMENT"] = hosting
    claims = []

    async def process(request, _app, _adapter):
        claims.append(request["claims_identity"])
        return web.Response(status=202)

    with patch("pmoagent.host.start_agent_process", process):
        app = build_app(settings, environ)
        app.cleanup_ctx.clear()
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/activity/messages", json={"type": "message"})
            assert response.status == expected
            assert (await client.post("/other", json={})).status in {401, 404}
    if expected == 202:
        assert len(claims) == 1 and claims[0].allow_anonymous
    else:
        assert claims == []


@pytest.mark.parametrize("environment", ["local", "azure"])
async def test_blob_initialization_at_startup(settings, monkeypatch, environment):
    settings.environment = environment
    if environment == "azure":
        settings.storage_connection_string = None
        settings.storage_account_name = "pmodemo"
    initialize = AsyncMock()
    monkeypatch.setattr(ConversationStore, "initialize", initialize)
    app = build_app(settings, {"FOUNDRY_AGENT_BLUEPRINT_CLIENT_ID": str(uuid4())})
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/readiness")).status == 200
        if environment == "local":
            initialize.assert_awaited_once_with()
        else:
            initialize.assert_not_called()


@pytest.mark.storage
async def test_blob_store_serializes_concurrent_approvals(settings):
    if "PMO_TEST_STORAGE_CONNECTION" not in os.environ:
        pytest.skip("Set PMO_TEST_STORAGE_CONNECTION to an isolated Azurite instance")
    settings.storage_container = f"test-{uuid4().hex[:16]}"
    async with DefaultAzureCredential() as credential:
        store = ConversationStore.from_settings(settings, credential)
        await store.initialize()
        try:
            draft = StoredDraft(
                version=1,
                draft_id="a" * 16,
                body="body",
                instruction="report",
                requester_id="alice-id",
                requester_name="Alice",
            )
            await store.update("k", lambda s: s.drafts.append(draft))

            def approve(state):
                if state.approvals:
                    raise ValueError("already approved")
                state.approvals.append(
                    Approval(
                        version=1,
                        draft_id=draft.draft_id,
                        approver_id="alice-id",
                        approved_at=draft.created_at,
                    )
                )

            results = await asyncio.gather(
                *(store.update("k", approve) for _ in range(5)), return_exceptions=True
            )
            assert sum(r is None for r in results) == 1
            state, etag = await store.load("k")
            assert len(state.approvals) == 1 and etag
            fresh, none = await store.load("missing")
            assert fresh.drafts == [] and none is None
        finally:
            await store.container.delete_container()
            await store.close()
