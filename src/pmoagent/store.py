import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from azure.core import MatchConditions
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.storage.blob.aio import BlobServiceClient, ContainerClient
from pydantic import BaseModel, ConfigDict, Field

from pmoagent.config import Settings

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(UTC)


class StoredDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    draft_id: str
    body: str
    instruction: str
    requester_id: str
    requester_name: str
    created_at: datetime = Field(default_factory=utcnow)


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    draft_id: str
    approver_id: str
    approved_at: datetime


class ConversationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drafts: list[StoredDraft] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)

    @property
    def latest(self) -> StoredDraft | None:
        return self.drafts[-1] if self.drafts else None

    def approval_of(self, draft: StoredDraft) -> Approval | None:
        return next(
            (
                a
                for a in self.approvals
                if (a.version, a.draft_id) == (draft.version, draft.draft_id)
            ),
            None,
        )


class ConflictError(Exception):
    """The conversation state changed concurrently too many times."""


class ConversationStore:
    def __init__(
        self, container: ContainerClient, service: BlobServiceClient | None = None
    ) -> None:
        self.container = container
        self._service = service

    @classmethod
    def from_settings(
        cls, settings: Settings, credential: AsyncTokenCredential
    ) -> "ConversationStore":
        if settings.storage_connection_string:
            service = BlobServiceClient.from_connection_string(settings.storage_connection_string)
        else:
            url = f"https://{settings.storage_account_name}.blob.core.windows.net"
            service = BlobServiceClient(url, credential=credential)
        return cls(service.get_container_client(settings.storage_container), service)

    async def initialize(self) -> None:
        try:
            await self.container.create_container()
        except ResourceExistsError:
            pass

    async def close(self) -> None:
        if self._service:
            await self._service.close()

    @staticmethod
    def _name(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest() + ".json"

    async def load(self, key: str) -> tuple[ConversationState, str | None]:
        try:
            download = await self.container.download_blob(self._name(key))
        except ResourceNotFoundError:
            return ConversationState(), None
        state = ConversationState.model_validate_json(await download.readall())
        return state, download.properties.etag

    async def update(self, key: str, mutate: Callable[[ConversationState], T]) -> T:
        blob = self.container.get_blob_client(self._name(key))
        for _ in range(3):
            state, etag = await self.load(key)
            result = mutate(state)
            data = state.model_dump_json()
            try:
                if etag:
                    await blob.upload_blob(
                        data,
                        overwrite=True,
                        etag=etag,
                        match_condition=MatchConditions.IfNotModified,
                    )
                else:
                    await blob.upload_blob(data, overwrite=False)
            except (ResourceModifiedError, ResourceExistsError):
                continue
            return result
        raise ConflictError("Conversation state is being updated concurrently")
