import asyncio
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx2
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import get_bearer_token_provider
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from openai import AsyncOpenAI
from openai.types.responses import FunctionToolParam
from opentelemetry import trace

from pmoagent.agent import SYSTEM_PROMPT, Evidence, ToolAccessError, report_prompt, tool_denial
from pmoagent.config import Settings
from pmoagent.report import Report, report_schema

logger = logging.getLogger(__name__)


@dataclass
class BoundTool:
    session: ClientSession
    name: str
    definition: FunctionToolParam


async def discover(
    settings: Settings, tokens: dict[str, str], stack: AsyncExitStack
) -> dict[str, BoundTool]:
    tools: dict[str, BoundTool] = {}
    for server in settings.servers:
        try:
            client = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {tokens[server.label]}"},
                    timeout=settings.generation_timeout,
                )
            )
            reader, writer = await stack.enter_async_context(
                streamable_http_client(server.url, http_client=client)
            )
            session = await stack.enter_async_context(ClientSession(reader, writer))
            await session.initialize()
            offered: list[str] = []
            cursor = None
            while True:
                page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
                for tool in page.tools:
                    offered.append(tool.name)
                    key = f"{server.label}__{tool.name}"
                    if key in settings.allowed_tools:
                        alias = f"read_{len(tools)}"
                        tools[alias] = BoundTool(
                            session,
                            key,
                            {
                                "type": "function",
                                "name": alias,
                                "description": f"{key}: {tool.description or ''}",
                                "parameters": tool.input_schema,
                                "strict": False,
                            },
                        )
                cursor = page.next_cursor
                if not cursor:
                    break
            logger.info("mcp_tools server=%s offered=%s", server.label, sorted(offered))
        except Exception:
            raise ToolAccessError(f"MCP server unavailable: {server.label}") from None
    logger.info("mcp_tools enabled=%s", sorted(tool.name for tool in tools.values()))
    return tools


class GptReportGenerator:
    def __init__(self, settings: Settings, credential: AsyncTokenCredential) -> None:
        self.settings = settings
        self.credential = credential

    async def generate(
        self, instruction: str, feedback: str, previous_draft: str, tokens: dict[str, str]
    ) -> Report:
        settings = self.settings
        assert settings.gpt_model
        messages: list[Any] = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
                + "\n次のスキーマの JSON オブジェクトだけを返してください。\n"
                + json.dumps(report_schema()),
            },
            {
                "role": "user",
                "content": report_prompt(
                    instruction, feedback, previous_draft, settings.ado_project
                ),
            },
        ]
        allowed = {"mcp__" + name for name in settings.allowed_tools}
        evidence = Evidence()
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("pmo.generate"):
            async with (
                asyncio.timeout(settings.generation_timeout),
                AsyncOpenAI(
                    base_url=settings.openai_base_url,
                    api_key=get_bearer_token_provider(
                        self.credential, "https://cognitiveservices.azure.com/.default"
                    ),
                ) as model,
            ):
                stack = AsyncExitStack()
                try:
                    tools = await discover(settings, tokens, stack)
                    if not tools:
                        raise ToolAccessError("No allowed MCP tools available")
                    corrections = 2
                    for _ in range(settings.max_turns):
                        # Reasoning models reject function tools on Chat Completions.
                        with tracer.start_as_current_span(
                            f"chat {settings.gpt_model}",
                            kind=trace.SpanKind.CLIENT,
                            attributes={
                                "gen_ai.operation.name": "chat",
                                "gen_ai.request.model": settings.gpt_model,
                            },
                        ) as span:
                            reply = await model.responses.create(
                                model=settings.gpt_model,
                                input=messages,
                                tools=[tool.definition for tool in tools.values()],
                                text={"format": {"type": "json_object"}},
                                store=False,
                                include=["reasoning.encrypted_content"],
                            )
                            if reply.usage:
                                span.set_attribute(
                                    "gen_ai.usage.input_tokens", reply.usage.input_tokens
                                )
                                span.set_attribute(
                                    "gen_ai.usage.output_tokens", reply.usage.output_tokens
                                )
                        calls = [item for item in reply.output if item.type == "function_call"]
                        if not calls:
                            if reply.status != "completed" or not reply.output_text:
                                raise ValueError("GPT did not return a complete report")
                            try:
                                report = Report.model_validate_json(reply.output_text)
                                evidence.validate(report)
                                return report
                            except ValueError as error:
                                logger.info("report_rejected reason=%s", str(error).splitlines()[0])
                                if corrections == 0:
                                    raise
                                corrections -= 1
                                messages.extend(
                                    item.model_dump(exclude_none=True) for item in reply.output
                                )
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": f"この報告は検証に失敗しました: {str(error)[:1000]}\n"
                                        "sources にはツール結果の文字列をそのまま書き写し、"
                                        "work_item_ids には取得した ID だけを入れて、"
                                        "スキーマどおりの JSON を返し直してください。"
                                        "根拠がない項目は削除してください。",
                                    }
                                )
                                continue
                        messages.extend(item.model_dump(exclude_none=True) for item in reply.output)
                        for call in calls:
                            if call.name not in tools:
                                raise ToolAccessError("Unknown GPT tool")
                            bound = tools[call.name]
                            arguments = json.loads(call.arguments)
                            name = "mcp__" + bound.name
                            if not isinstance(arguments, dict) or tool_denial(
                                name, arguments, allowed, settings.ado_project
                            ):
                                raise ToolAccessError("GPT tool arguments or project not allowed")
                            with tracer.start_as_current_span(
                                f"execute_tool {bound.name}",
                                kind=trace.SpanKind.CLIENT,
                                attributes={
                                    "gen_ai.operation.name": "execute_tool",
                                    "gen_ai.tool.name": bound.name,
                                },
                            ) as span:
                                result = await bound.session.call_tool(
                                    bound.name.split("__", 1)[1], arguments
                                )
                                if not isinstance(result, types.CallToolResult):
                                    raise ToolAccessError("MCP tool did not succeed")
                                if result.is_error:
                                    span.set_status(trace.StatusCode.ERROR)
                            logger.info("mcp_call tool=%s error=%s", bound.name, result.is_error)
                            text = "\n".join(
                                block.text
                                for block in result.content
                                if isinstance(block, types.TextContent)
                            )
                            if result.structured_content is not None:
                                text += "\n" + json.dumps(
                                    result.structured_content, ensure_ascii=False
                                )
                            # Tool errors (e.g. a misnamed project) go back to the model, not evidence.
                            if not result.is_error:
                                evidence.add(name, text)
                            messages.append(
                                {
                                    "type": "function_call_output",
                                    "call_id": call.call_id,
                                    "output": text,
                                }
                            )
                finally:
                    await stack.aclose()
        raise ValueError("GPT exceeded the report turn limit")
