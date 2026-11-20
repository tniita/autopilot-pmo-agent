from typing import Any

import pytest

from pmoagent.agent import ReportGenerator
from pmoagent.bot import PmoBot
from pmoagent.config import ADO_SCOPE, AGENT365_SCOPE
from tests.conftest import MemoryStore, fake_query

KEY = "instance:tenant:conversation"


def make_bot(settings, calls: list[dict[str, Any]] | None = None, **query_args: Any):
    store = MemoryStore()
    generator = ReportGenerator(settings, fake_query(calls=calls, **query_args))
    return PmoBot(settings, store, generator), store  # type: ignore[arg-type]


async def tokens(scope: str) -> str:
    return {ADO_SCOPE: "ado-token", AGENT365_SCOPE: "m365-token"}[scope]


async def say(bot, text: str, sender: str = "alice-id", name: str = "Alice") -> str:
    return await bot.respond(text, sender, name, KEY, tokens)


async def test_requester_only_approval_in_group_chat(settings):
    bot, store = make_bot(settings)
    draft = await say(bot, "今週の進捗をまとめて")
    assert draft.startswith("【PMO報告ドラフト v1】") and "依頼者: Alice" in draft

    refused = await say(bot, "承認", sender="bob-id", name="Bob")
    assert "依頼者（Alice）だけ" in refused
    assert store.states[KEY].approvals == []

    record = await say(bot, "承認")
    stored = store.states[KEY].drafts[-1]
    assert record.startswith(f"【承認記録】v1 ドラフトID: {stored.draft_id}")
    assert "承認者: Alice" in record
    assert store.states[KEY].approvals[0].approver_id == "alice-id"

    assert "承認済み" in await say(bot, "承認")
    assert len(store.states[KEY].approvals) == 1


async def test_revision_is_requester_only_and_keeps_original_request(settings):
    calls: list[dict[str, Any]] = []
    bot, store = make_bot(settings, calls)
    await say(bot, "リリース準備の状況を報告して")

    assert "依頼者（Alice）だけ" in await say(bot, "差戻し: 詳しく", sender="bob-id", name="Bob")
    assert len(calls) == 1

    revised = await say(bot, "差戻し: リスクを詳しく")
    assert revised.startswith("【PMO報告ドラフト v2】")
    prompt = calls[-1]["prompt"]
    assert "リスクを詳しく" in prompt and "リリース準備の状況を報告して" in prompt
    drafts = store.states[KEY].drafts
    assert [d.version for d in drafts] == [1, 2] and drafts[1].requester_id == "alice-id"


async def test_generation_gets_time_and_location_but_stores_original_request(settings):
    calls: list[dict[str, Any]] = []
    bot, store = make_bot(settings, calls)
    where = "Teamsチャット（chat-id: 19:group@thread.v2）"
    await bot.respond("進捗をまとめて", "alice-id", "Alice", KEY, tokens, location=where)
    prompt = calls[-1]["prompt"]
    assert where in prompt and "現在日時: 20" in prompt and "+09:00" in prompt
    assert store.states[KEY].drafts[-1].instruction == "進捗をまとめて"


async def test_conversations_are_isolated(settings):
    bot, _ = make_bot(settings)
    await say(bot, "今週の報告")
    other = await bot.respond("承認", "alice-id", "Alice", "instance:tenant:other", tokens)
    assert "対象のドラフトがありません" in other


@pytest.mark.parametrize("text", ["承認", "差戻し: 理由"])
async def test_commands_without_draft_are_refused(settings, text):
    calls: list[dict[str, Any]] = []
    bot, _ = make_bot(settings, calls)
    assert "対象のドラフトがありません" in await say(bot, text)
    assert calls == []


async def test_token_failure_is_reported_without_generation(settings):
    calls: list[dict[str, Any]] = []
    bot, store = make_bot(settings, calls)

    async def failing(_: str) -> str:
        raise RuntimeError("exchange failed")

    answer = await bot.respond("報告して", "alice-id", "Alice", KEY, failing)
    assert "接続できませんでした" in answer and calls == [] and KEY not in store.states


async def test_ungrounded_report_is_not_saved(settings):
    bot, store = make_bot(settings, ado_result="no ids here")
    answer = await say(bot, "報告して")
    assert "根拠を確認できる報告を作成できませんでした" in answer
    assert KEY not in store.states
