import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from azure.identity.aio import DefaultAzureCredential
from microsoft_agents.activity import Activity, ChannelAccount, ConversationAccount

from pmoagent.host import build_app, conversation_key, handle_message, sdk_configuration
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


def test_sdk_configuration_uses_platform_blueprint_and_agentic_handler():
    config = sdk_configuration({"FOUNDRY_AGENT_BLUEPRINT_CLIENT_ID": "blueprint"})
    connection = config["CONNECTIONS"]["SERVICE_CONNECTION"]["SETTINGS"]
    assert connection["AUTHTYPE"] == "IdentityProxyManager"
    assert connection["CLIENTID"] == "blueprint"
    handler = config["AGENTAPPLICATION"]["USERAUTHORIZATION"]["HANDLERS"]["AGENTIC"]
    assert handler["TYPE"] == "AgenticUserAuthorization"


async def test_activity_endpoint_requires_jwt(settings):
    app = build_app(settings, {"FOUNDRY_AGENT_BLUEPRINT_CLIENT_ID": str(uuid4())})
    app.cleanup_ctx.clear()
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/readiness")).status == 200
        for path in ["/activity/messages", "/api/messages"]:
            response = await client.post(path, json={"type": "message"})
            assert response.status == 401


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
