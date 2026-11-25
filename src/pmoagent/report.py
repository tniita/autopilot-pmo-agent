import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DRAFT_PREFIX = "【PMO報告ドラフト v"
APPROVAL_PREFIX = "【承認記録】"
_REVISE_RE = re.compile(r"^差戻し\s*[:：]\s*(.+)$", re.S)
MAX_DRAFT_BYTES = 20000


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _unwrap_text(value: str) -> str:
    # Models sometimes wrap a plain field as {"text": "..."}; keep only the sentence.
    stripped = value.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return value
        if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
            return str(parsed["text"])
    return value


class Finding(BaseModel):
    # The JSON schema mirrors require_evidence so the model sees the rule up front.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "anyOf": [
                {
                    "required": ["work_item_ids"],
                    "properties": {"work_item_ids": {"type": "array", "minItems": 1}},
                },
                {
                    "required": ["sources"],
                    "properties": {"sources": {"type": "array", "minItems": 1}},
                },
            ]
        },
    )

    text: str = Field(min_length=1, max_length=220)
    work_item_ids: list[int] = Field(default_factory=list, max_length=8)
    sources: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("text")
    @classmethod
    def flatten_text(cls, value: str) -> str:
        return _single_line(value)

    @field_validator("sources")
    @classmethod
    def flatten_sources(cls, values: list[str]) -> list[str]:
        flattened = [_single_line(value) for value in values]
        if any(not 3 <= len(value) <= 300 for value in flattened):
            raise ValueError("Each source must be 3-300 characters")
        return flattened

    @model_validator(mode="after")
    def require_evidence(self) -> "Finding":
        if not self.work_item_ids and not self.sources:
            raise ValueError("Each finding needs a work item ID or a Microsoft 365 source")
        if any(item_id < 1 for item_id in self.work_item_ids):
            raise ValueError("Work item IDs must be positive")
        return self


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        min_length=1,
        max_length=600,
        description="報告の要点。JSONではなく日本語の文章で書く。",
    )
    progress: list[Finding] = Field(max_length=4)
    issues: list[Finding] = Field(max_length=4)
    risks: list[Finding] = Field(max_length=4)
    next_actions: list[Finding] = Field(max_length=4)
    limitations: str = Field(
        min_length=1,
        max_length=300,
        description="読み手向けに、確認できなかった情報を日本語の文章で簡潔に書く。JSONにしない。",
    )

    @field_validator("summary", "limitations")
    @classmethod
    def flatten(cls, value: str) -> str:
        return _single_line(_unwrap_text(value))

    def findings(self) -> list[Finding]:
        return self.progress + self.issues + self.risks + self.next_actions


@dataclass(frozen=True)
class RenderedDraft:
    text: str
    body: str
    draft_id: str


@dataclass(frozen=True)
class Command:
    kind: Literal["request", "approve", "revise"]
    text: str


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _evidence(finding: Finding) -> str:
    parts = [f"#{item_id}" for item_id in finding.work_item_ids]
    parts += [f"「{source}」" for source in finding.sources]
    return "、".join(parts)


def render_draft(report: Report, version: int, requester: str) -> RenderedDraft:
    lines = [f"{DRAFT_PREFIX}{version}】", "", f"**概要**: {report.summary}"]
    for label, findings in [
        ("進捗", report.progress),
        ("課題", report.issues),
        ("リスク（推測を含む）", report.risks),
        ("次のアクション（提案）", report.next_actions),
    ]:
        lines += ["", f"**{label}**"]
        lines += [f"- {f.text}（根拠: {_evidence(f)}）" for f in findings] or ["- 該当なし"]
    lines += ["", f"**制約**: {report.limitations}"]
    body = "\n".join(lines)
    draft_id = _digest(body)
    text = (
        f"{body}\n---\nドラフトID: {draft_id}　依頼者: {_single_line(requester)}\n"
        "AIが作成した下書きです。依頼者は内容を確認し、承認する場合は「承認」、"
        "修正する場合は「差戻し: 理由」とメンション付きで返信してください。"
    )
    if len(text.encode()) > MAX_DRAFT_BYTES:
        raise ValueError("Draft exceeds the Teams message budget")
    return RenderedDraft(text=text, body=body, draft_id=draft_id)


def approval_record(version: int, draft_id: str, approver: str, approved_at: datetime) -> str:
    return (
        f"{APPROVAL_PREFIX}v{version} ドラフトID: {draft_id}\n"
        f"承認者: {_single_line(approver)}\n"
        f"承認日時(UTC): {approved_at.isoformat(timespec='seconds')}"
    )


def parse_command(text: str) -> Command:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if normalized in {"承認", "承認します"}:
        return Command("approve", "")
    revise = _REVISE_RE.match(normalized)
    if revise:
        return Command("revise", revise.group(1).strip()[:1000])
    return Command("request", normalized)


def report_schema() -> dict[str, object]:
    schema = Report.model_json_schema()
    # Validate the schema is JSON-serializable before handing it to the CLI.
    json.dumps(schema)
    return schema
