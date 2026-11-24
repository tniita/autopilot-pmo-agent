import copy
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pmoagent.config import Settings
from pmoagent.store import ConversationState

_SPANS = InMemorySpanExporter()

AZURITE = (
    "DefaultEndpointsProtocol=http;AccountName=pmolocal;"
    "AccountKey=cG1vLWxvY2FsLWRldmVsb3BtZW50LW9ubHktdGVzdC1rZXk=;"
    "BlobEndpoint=http://127.0.0.1:10000/pmolocal;"
)

REPORT = {
    "summary": "Sprint is on track.",
    "progress": [{"text": "Login feature is active.", "work_item_ids": [42], "sources": []}],
    "issues": [],
    "risks": [{"text": "Review raised scope risk.", "sources": ["Sprint Review 11/14"]}],
    "next_actions": [],
    "limitations": "Only recent items were reviewed.",
}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        foundry_project_endpoint="https://example.services.ai.azure.com/api/projects/pmo",
        anthropic_model="test-claude",
        ado_organization="contoso",
        storage_connection_string=os.getenv("PMO_TEST_STORAGE_CONNECTION", AZURITE),
    )


@pytest.fixture
def spans() -> InMemorySpanExporter:
    # The global tracer provider can be set only once per process.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_SPANS))
        trace.set_tracer_provider(provider)
    _SPANS.clear()
    return _SPANS


def fake_query(
    report: dict[str, Any] | None = None,
    ado_result: str = '{"id": 42, "title": "Login"}',
    m365_result: str = "Meeting: Sprint Review 11/14 - scope risk discussed",
    statuses: dict[str, str] | None = None,
    calls: list[dict[str, Any]] | None = None,
) -> Callable[..., AsyncIterator[Any]]:
    async def run(**kwargs: Any) -> AsyncIterator[Any]:
        if calls is not None:
            calls.append(kwargs)
        servers = statuses or {"ado": "connected", "teams": "connected", "calendar": "connected"}
        yield SystemMessage(
            subtype="init",
            data={"mcp_servers": [{"name": n, "status": s} for n, s in servers.items()]},
        )
        yield AssistantMessage(
            content=[
                ToolUseBlock(id="t1", name="mcp__ado__wit_work_item", input={}),
                ToolUseBlock(id="t2", name="mcp__teams__ListChatMessages", input={}),
            ],
            model="test-claude",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(tool_use_id="t1", content=ado_result, is_error=False),
                ToolResultBlock(tool_use_id="t2", content=[{"type": "text", "text": m365_result}]),
            ]
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=2,
            session_id="test",
            structured_output=report if report is not None else REPORT,
        )

    return run


class MemoryStore:
    """Mirrors ConversationStore's load/update semantics without Azure Storage."""

    def __init__(self) -> None:
        self.states: dict[str, ConversationState] = {}

    async def load(self, key: str) -> tuple[ConversationState, str | None]:
        state = self.states.get(key)
        return (copy.deepcopy(state), "etag") if state else (ConversationState(), None)

    async def update(self, key: str, mutate: Callable[[ConversationState], Any]) -> Any:
        state, _ = await self.load(key)
        result = mutate(state)
        self.states[key] = state
        return result
