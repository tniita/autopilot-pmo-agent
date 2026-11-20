import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from opentelemetry import trace

from pmoagent.agent import ReportGenerator, ToolAccessError
from pmoagent.config import Settings
from pmoagent.gpt import GptReportGenerator
from pmoagent.report import Report, approval_record, parse_command, render_draft
from pmoagent.store import Approval, ConversationState, ConversationStore, StoredDraft

logger = logging.getLogger(__name__)
TokenProvider = Callable[[str], Awaitable[str]]

NO_DRAFT = "対象のドラフトがありません。先に報告を依頼してください。"


class WorkflowRejection(Exception):
    """A user-visible refusal decided while updating state."""


class PmoBot:
    def __init__(
        self,
        settings: Settings,
        store: ConversationStore,
        generator: ReportGenerator | GptReportGenerator,
    ):
        self.settings = settings
        self.store = store
        self.generator = generator

    async def respond(
        self,
        text: str,
        sender_id: str,
        sender_name: str,
        conversation_key: str,
        tokens: TokenProvider,
        location: str = "",
    ) -> str:
        command = parse_command(text)
        if not command.text and command.kind == "request":
            return "依頼内容を入力してください。例: 今週の進捗とリスクをまとめて"
        try:
            if command.kind == "approve":
                return await self._approve(conversation_key, sender_id, sender_name)
            state, _ = await self.store.load(conversation_key)
            latest = state.latest
            if command.kind == "revise":
                if latest is None:
                    return NO_DRAFT
                if latest.requester_id != sender_id:
                    return f"差戻しできるのは依頼者（{latest.requester_name}）だけです。"
                report = await self._generate(
                    latest.instruction, command.text, latest.body, tokens, location
                )
                return await self._save(conversation_key, report, latest.instruction, latest)
            report = await self._generate(command.text, "", "", tokens, location)
            requester = StoredDraft(
                version=0,
                draft_id="",
                body="",
                instruction=command.text,
                requester_id=sender_id,
                requester_name=sender_name,
            )
            return await self._save(conversation_key, report, command.text, requester)
        except WorkflowRejection as rejection:
            return str(rejection)
        except ToolAccessError:
            logger.exception("Data source unavailable")
            return (
                "Azure DevOps／Microsoft 365に接続できませんでした。"
                "エージェントのアカウントの権限と管理者の同意を確認してください。"
            )
        except TimeoutError:
            logger.exception("Report generation timed out")
            return "報告の作成がタイムアウトしました。対象を絞って再度依頼してください。"
        except Exception:
            logger.exception("Request failed")
            return "根拠を確認できる報告を作成できませんでした。依頼内容を具体的にして再度お試しください。"

    async def _generate(
        self,
        instruction: str,
        feedback: str,
        previous: str,
        tokens: TokenProvider,
        location: str = "",
    ) -> Report:
        acquired: dict[str, str] = {}
        for server in self.settings.servers:
            try:
                acquired[server.label] = await tokens(server.scope)
            except Exception as error:
                raise ToolAccessError(f"Token acquisition failed for {server.label}") from error
        now = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="minutes")
        context = f"\n\n（実行時の情報）現在日時: {now}"
        if location:
            context += f"。この依頼を受けた場所: {location}。会話を参照するときはこの場所を使います"
        return await self.generator.generate(instruction + context, feedback, previous, acquired)

    async def _save(
        self, key: str, report: Report, instruction: str, requester: StoredDraft
    ) -> str:
        def add(state: ConversationState) -> str:
            version = (state.latest.version if state.latest else 0) + 1
            draft = render_draft(report, version, requester.requester_name)
            state.drafts.append(
                StoredDraft(
                    version=version,
                    draft_id=draft.draft_id,
                    body=draft.body,
                    instruction=instruction,
                    requester_id=requester.requester_id,
                    requester_name=requester.requester_name,
                )
            )
            return draft.text

        return await self.store.update(key, add)

    async def _approve(self, key: str, sender_id: str, sender_name: str) -> str:
        approved_at = datetime.now(UTC)

        def approve(state: ConversationState) -> tuple[StoredDraft, Approval]:
            latest = state.latest
            if latest is None:
                raise WorkflowRejection(NO_DRAFT)
            if latest.requester_id != sender_id:
                raise WorkflowRejection(
                    f"このドラフトを承認できるのは依頼者（{latest.requester_name}）だけです。"
                )
            if state.approval_of(latest):
                raise WorkflowRejection(
                    f"v{latest.version}（ドラフトID: {latest.draft_id}）は承認済みです。"
                )
            approval = Approval(
                version=latest.version,
                draft_id=latest.draft_id,
                approver_id=sender_id,
                approved_at=approved_at,
            )
            state.approvals.append(approval)
            return latest, approval

        draft, approval = await self.store.update(key, approve)
        trace.get_current_span().add_event(
            "pmo.approval",
            {
                "pmo.draft_version": approval.version,
                "pmo.draft_id": approval.draft_id,
                "pmo.approver_id": approval.approver_id,
            },
        )
        logger.info("Draft approved: version=%s draft_id=%s", draft.version, draft.draft_id)
        return approval_record(draft.version, draft.draft_id, sender_name, approved_at)
