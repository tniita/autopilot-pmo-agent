import json
from typing import Any

import pytest
from pydantic import SecretStr

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
    assert "ANTHROPIC_FOUNDRY_API_KEY" not in options.env
    assert json.loads(call["prompt"])["instruction"] == "今週の報告"


async def test_claude_api_key_replaces_entra_auth(settings):
    settings.runtime_client_id = "runtime-client"
    settings.anthropic_api_key = SecretStr("foundry-key")
    _, call = await generate(settings)
    env = call["options"].env
    assert env["ANTHROPIC_FOUNDRY_API_KEY"] == "foundry-key"
    assert "AZURE_CLIENT_ID" not in env
    assert "foundry-key" not in repr(settings)


async def guard_decision(settings, tool: str, tool_input: dict[str, Any]) -> str | None:
    _, call = await generate(settings)
    guard = call["options"].hooks["PreToolUse"][0].hooks[0]
    output = await guard({"tool_name": tool, "tool_input": tool_input}, "id", None)
    return output.get("hookSpecificOutput", {}).get("permissionDecision")


@pytest.mark.parametrize(
    ("tool", "decision"),
    [
        ("mcp__ado__wit_work_item", None),
        ("mcp__teams__ListChatMessages", None),
        ("mcp__calendar__ListCalendarView", None),
        ("mcp__ado__wit_work_item_write", "deny"),
        ("mcp__teams__SendMessageToChat", "deny"),
        ("mcp__teams__SearchTeamsMessages", "deny"),
        ("mcp__calendar__CreateEvent", "deny"),
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


UNGROUNDED = {**REPORT, "next_actions": [{"text": "提案: 判定会で確認する"}]}


def scripted_query(reports: list[dict[str, Any]], calls: list[dict[str, Any]]):
    async def run(**kwargs: Any):
        calls.append(kwargs)
        source = fake_query(report=reports[min(len(calls), len(reports)) - 1])
        async for message in source(**kwargs):
            yield message

    return run


async def test_claude_retries_ungrounded_report_in_the_same_session(settings):
    calls: list[dict[str, Any]] = []
    generator = ReportGenerator(settings, scripted_query([UNGROUNDED, REPORT], calls))
    report = await generator.generate("今週の報告", "", "", TOKENS)
    assert report.summary == REPORT["summary"] and len(calls) == 2
    assert calls[0]["options"].resume is None and calls[1]["options"].resume == "test"
    assert calls[1]["options"].env["CLAUDE_CONFIG_DIR"].startswith(calls[0]["options"].cwd)
    assert "検証に失敗しました" in calls[1]["prompt"]


async def test_claude_gives_up_after_two_corrections(settings):
    calls: list[dict[str, Any]] = []
    generator = ReportGenerator(settings, scripted_query([UNGROUNDED], calls))
    with pytest.raises(ValueError):
        await generator.generate("今週の報告", "", "", TOKENS)
    assert len(calls) == 3
