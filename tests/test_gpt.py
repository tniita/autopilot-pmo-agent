import json
from contextlib import AsyncExitStack, asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mcp import types
from openai import AsyncOpenAI

from pmoagent.agent import ToolAccessError
from pmoagent.gpt import GptReportGenerator
from tests.conftest import REPORT


@pytest.mark.parametrize(
    "case", ["ok", "unknown", "wrong-project", "bad-citation", "fixed-citation", "tool-error"]
)
async def test_gpt_report(settings, case):
    settings = settings.model_copy(
        update={"provider": "gpt", "gpt_model": "demo-deployment", "ado_project": "PMO"}
    )
    exchanges = []
    tokens = {server.label: f"private-{server.label}-token" for server in settings.servers}
    sessions = []

    @asynccontextmanager
    async def transport(url, *, http_client):
        server = next(server for server in settings.servers if server.url == url)
        assert http_client.headers["Authorization"] == f"Bearer {tokens[server.label]}"
        yield server.label, None

    @asynccontextmanager
    async def session(label, _writer):
        client = AsyncMock()

        def tool(name):
            return types.Tool(name=name, inputSchema={"type": "object", "properties": {}})

        client.list_tools.side_effect = [
            types.ListToolsResult(tools=[tool("forbidden_write")], nextCursor="more"),
            types.ListToolsResult(tools=[tool("wit_work_item")] if label == "ado" else []),
        ]
        client.call_tool.return_value = types.CallToolResult(
            content=[types.TextContent(type="text", text="item #42")], isError=case == "tool-error"
        )
        sessions.append(client)
        yield client

    def respond(request):
        body = json.loads(request.content)
        assert all(secret not in str(body) for secret in tokens.values())
        assert body["model"] == "demo-deployment" and body["store"] is False
        assert request.url.path.endswith("/responses")
        assert len(body["tools"]) == 1 and "wit_work_item" in str(body["tools"])
        exchanges.append(body)
        if len(exchanges) == 1:
            name = "forbidden_write" if case == "unknown" else body["tools"][0]["name"]
            args = {"project": "other" if case == "wrong-project" else "pmo"}
            output = [
                {
                    "type": "function_call",
                    "id": "fc1",
                    "call_id": "call1",
                    "name": name,
                    "arguments": json.dumps(args),
                    "status": "completed",
                }
            ]
        else:
            if len(exchanges) == 2:
                assert body["input"][-2]["type"] == "function_call"
                assert body["input"][-1] == {
                    "type": "function_call_output",
                    "call_id": "call1",
                    "output": "item #42",
                }
            else:
                assert body["input"][-1]["role"] == "user"
                assert "検証に失敗" in body["input"][-1]["content"]
            uncited = case == "bad-citation" or (case == "fixed-citation" and len(exchanges) == 2)
            report = REPORT if uncited else {**REPORT, "risks": []}
            output = [
                {
                    "type": "message",
                    "id": "msg1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": json.dumps(report), "annotations": []}
                    ],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": "result",
                "object": "response",
                "created_at": 1,
                "model": "demo",
                "status": "completed",
                "output": output,
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
            },
        )

    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )

        def model(**kwargs):
            assert kwargs["base_url"] == settings.openai_base_url and callable(kwargs["api_key"])
            return AsyncOpenAI(
                base_url=kwargs["base_url"], api_key="test-inference", http_client=http
            )

        stack.enter_context(patch("pmoagent.gpt.AsyncOpenAI", model))
        stack.enter_context(patch("pmoagent.gpt.streamable_http_client", transport))
        stack.enter_context(patch("pmoagent.gpt.ClientSession", session))
        generator = GptReportGenerator(settings, MagicMock())
        pending = generator.generate("request", "feedback", "previous", tokens)
        if case in {"ok", "fixed-citation"}:
            assert (await pending).progress[0].work_item_ids == [42]
            assert len(exchanges) == (3 if case == "fixed-citation" else 2)
        else:
            failure = ValueError if case in {"bad-citation", "tool-error"} else ToolAccessError
            with pytest.raises(failure):
                await pending
            if case == "bad-citation":
                assert len(exchanges) == 4
        if case in {"unknown", "wrong-project"}:
            sessions[0].call_tool.assert_not_awaited()
        else:
            sessions[0].call_tool.assert_awaited_once_with("wit_work_item", {"project": "pmo"})
