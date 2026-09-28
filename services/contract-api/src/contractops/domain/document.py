from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class FindingKind(StrEnum):
    AMOUNT = "AMOUNT"
    DATE = "DATE"
    RISK = "RISK"
    OBLIGATION = "OBLIGATION"


class FindingOrigin(StrEnum):
    RULE = "RULE"
    MODEL = "MODEL"


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    id: UUID
    contract_version_id: UUID
    sequence: int
    page_number: int | None
    heading_path: tuple[str, ...]
    content: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class DocumentFinding:
    id: UUID
    contract_version_id: UUID
    source_chunk_id: UUID
    kind: FindingKind
    origin: FindingOrigin
    title: str
    normalized_value: str | None
    source_page: int | None
    source_text: str
    confidence: float
    model_name: str | None
    model_output_hash: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DiffEntry:
    change: str
    old_chunk: DocumentChunk | None
    new_chunk: DocumentChunk | None


@dataclass(frozen=True, slots=True)
class VersionDiff:
    contract_id: UUID
    from_version_id: UUID
    to_version_id: UUID
    entries: tuple[DiffEntry, ...]
