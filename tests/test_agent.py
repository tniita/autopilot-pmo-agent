import json
from typing import Any

import pytest

from pmoagent.agent import ReportGenerator, ToolAccessError
from tests.conftest import REPORT, fake_query

TOKENS = {"ado": "ado-token", "teams": "m365-token", "calendar": "m365-token"}


async def generate(settings, **query_args: Any):
    calls: list[dict[str, Any]] = []
    report = await ReportGenerator(settings, fake_query(calls=calls, **query_args)).generate(
        "今週の報告", "", "", TOKENS
    )
    return report, calls[0]


async def test_claude_runs_with_agent_user_tokens_and_read_only_allow_list(settings):
    settings.runtime_client_id = "runtime-client"
    report, call = await generate(settings)
    assert report.summary == REPORT["summary"]
    options = call["options"]
    assert options.tools == [] and options.setting_sources == []
    assert options.strict_mcp_config and options.permission_mode == "dontAsk"
    assert "mcp__ado__wit_work_item" in options.allowed_tools
    assert all(not name.endswith("_write") for name in options.allowed_tools)
    assert options.mcp_servers["ado"]["headers"] == {"Authorization": "Bearer ado-token"}
    assert options.mcp_servers["teams"]["url"].endswith("/mcp_TeamsServer")
    assert options.env["AZURE_CLIENT_ID"] == "runtime-client"
    assert json.loads(call["prompt"])["instruction"] == "今週の報告"


async def guard_decision(settings, tool: str, tool_input: dict[str, Any]) -> str | None:
    _, call = await generate(settings)
    guard = call["options"].hooks["PreToolUse"][0].hooks[0]
    output = await guard({"tool_name": tool, "tool_input": tool_input}, "id", None)
    return output.get("hookSpecificOutput", {}).get("permissionDecision")


@pytest.mark.parametrize(
    ("tool", "decision"),
    [
        ("mcp__ado__wit_work_item", None),
        ("mcp__ado__wit_work_item_write", "deny"),
        ("mcp__teams__mcp_graph_chat_postMessage", "deny"),
        ("mcp__calendar__mcp_CalendarTools_graph_createEvent", "deny"),
        ("mcp__other__anything", "deny"),
    ],
)
async def test_guard_blocks_writes_and_unknown_tools(settings, tool, decision):
    assert await guard_decision(settings, tool, {}) == decision


async def test_optional_project_guard(settings):
    settings.ado_project = "Contoso"
    assert await guard_decision(settings, "mcp__ado__wit_work_item", {"project": "contoso"}) is None
    assert await guard_decision(settings, "mcp__ado__wit_work_item", {"project": "Other"}) == "deny"


@pytest.mark.parametrize(
    "query_args",
    [
        {"report": {**REPORT, "progress": [{"text": "x", "work_item_ids": [4200]}]}},
        {"ado_result": '{"id": 420}'},
        {"report": {**REPORT, "risks": [{"text": "x", "sources": ["Unseen meeting"]}]}},
        {"report": {"summary": "missing fields"}},
    ],
)
async def test_ungrounded_or_invalid_reports_are_rejected(settings, query_args):
    with pytest.raises(ValueError):
        await generate(settings, **query_args)


async def test_disconnected_mcp_server_fails_closed(settings):
    statuses = {"ado": "connected", "teams": "failed", "calendar": "connected"}
    with pytest.raises(ToolAccessError):
        await generate(settings, statuses=statuses)
