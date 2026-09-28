from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

from contractops.domain.document import FindingKind, FindingOrigin
from contractops.errors import ContractOpsError

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_AMOUNT_PATTERN = re.compile(
    r"(?P<currency>RMB|CNY|USD|EUR|人民币|美元|欧元|¥|￥|\$|€)?\s*"
    r"(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*"
    r"(?P<unit>元|万元|百万|million)?",
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})[年./-](0[1-9]|1[0-2]|[1-9])[月./-]"
    r"(0[1-9]|[12]\d|3[01]|[1-9])日?(?!\d)"
)
_MAX_DOCX_ENTRIES = 2_048
_MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_MAX_DOCX_XML_BYTES = 10 * 1024 * 1024
_MAX_DOCX_COMPRESSION_RATIO = 200


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    sequence: int
    page_number: int | None
    heading_path: tuple[str, ...]
    content: str


@dataclass(frozen=True, slots=True)
class FindingCandidate:
    kind: FindingKind
    origin: FindingOrigin
    title: str
    normalized_value: str | None
    source_text: str
    confidence: float
    model_name: str | None = None
    model_output_hash: str | None = None


def parse_document(content: bytes, media_type: str) -> tuple[ParsedBlock, ...]:
    if media_type == "application/pdf":
        return _parse_pdf(content)
    if media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _parse_docx(content)
    raise ContractOpsError(
        code="document_media_type_unsupported",
        message="only PDF and DOCX documents are supported",
        status_code=415,
    )


def _parse_pdf(content: bytes) -> tuple[ParsedBlock, ...]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        blocks: list[ParsedBlock] = []
        for page_number, page in enumerate(reader.pages, start=1):
            raw_lines = (page.extract_text() or "").splitlines()
            text = "\n".join(line.strip() for line in raw_lines if line.strip())
            if text:
                blocks.append(
                    ParsedBlock(
                        sequence=len(blocks),
                        page_number=page_number,
                        heading_path=(),
                        content=text,
                    )
                )
        return tuple(blocks)
    except Exception as error:
        raise ContractOpsError(
            code="document_parse_failed",
            message="the PDF could not be parsed",
            status_code=422,
        ) from error


def _parse_docx(content: bytes) -> tuple[ParsedBlock, ...]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > _MAX_DOCX_ENTRIES:
                raise ValueError("DOCX contains too many archive entries")
            total_size = sum(member.file_size for member in members)
            if total_size > _MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError("DOCX expands beyond the processing limit")
            for member in members:
                if member.flag_bits & 0x1:
                    raise ValueError("encrypted DOCX entries are not supported")
                if member.file_size and (
                    member.compress_size == 0
                    or member.file_size / member.compress_size > _MAX_DOCX_COMPRESSION_RATIO
                ):
                    raise ValueError("DOCX compression ratio exceeds the processing limit")
            document = archive.getinfo("word/document.xml")
            if document.file_size > _MAX_DOCX_XML_BYTES:
                raise ValueError("DOCX document XML exceeds the processing limit")
            root = ElementTree.fromstring(archive.read(document))
    except (KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise ContractOpsError(
            code="document_parse_failed",
            message="the DOCX could not be parsed",
            status_code=422,
        ) from error

    headings: list[str] = []
    blocks: list[ParsedBlock] = []
    for paragraph in root.iter(f"{_WORD_NS}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t")).strip()
        if not text:
            continue
        style_node = paragraph.find(f"{_WORD_NS}pPr/{_WORD_NS}pStyle")
        style = "" if style_node is None else style_node.attrib.get(f"{_WORD_NS}val", "")
        heading_match = re.fullmatch(r"Heading([1-9])", style, re.IGNORECASE)
        if heading_match:
            level = int(heading_match.group(1))
            headings[level - 1 :] = [text]
        blocks.append(
            ParsedBlock(
                sequence=len(blocks),
                page_number=None,
                heading_path=tuple(headings),
                content=text,
            )
        )
    return tuple(blocks)


def extract_rule_findings(block: ParsedBlock) -> tuple[FindingCandidate, ...]:
    candidates: list[FindingCandidate] = []
    for match in _AMOUNT_PATTERN.finditer(block.content):
        currency = (match.group("currency") or "").upper()
        unit = match.group("unit") or ""
        if not currency and not unit:
            continue
        raw_amount = match.group("amount").replace(",", "")
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation:
            continue
        if unit == "万元":
            multiplier = Decimal("10000")
        elif unit in {"百万", "million"}:
            multiplier = Decimal("1000000")
        else:
            multiplier = Decimal("1")
        candidates.append(
            FindingCandidate(
                kind=FindingKind.AMOUNT,
                origin=FindingOrigin.RULE,
                title="Detected monetary amount",
                normalized_value=str(amount * multiplier),
                source_text=match.group(0).strip(),
                confidence=0.98,
            )
        )
    for match in _DATE_PATTERN.finditer(block.content):
        year, month, day = (int(value) for value in match.groups())
        candidates.append(
            FindingCandidate(
                kind=FindingKind.DATE,
                origin=FindingOrigin.RULE,
                title="Detected calendar date",
                normalized_value=f"{year:04d}-{month:02d}-{day:02d}",
                source_text=match.group(0),
                confidence=0.99,
            )
        )
    return tuple(candidates)


def validate_model_candidates(
    raw: object,
    *,
    source_text: str,
    model_name: str,
) -> tuple[FindingCandidate, ...]:
    """Fail closed: one malformed model item rejects the complete model response."""
    if not isinstance(raw, list):
        return ()
    validated: list[FindingCandidate] = []
    for item in raw:
        if not isinstance(item, dict):
            return ()
        value = dict(item)
        if set(value) - {"kind", "title", "source_text", "confidence", "normalized_value"}:
            return ()
        if value.get("kind") not in {FindingKind.RISK.value, FindingKind.OBLIGATION.value}:
            return ()
        quoted = value.get("source_text")
        title = value.get("title")
        confidence = value.get("confidence")
        if (
            not isinstance(quoted, str)
            or not quoted.strip()
            or quoted not in source_text
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(confidence, int | float)
            or not 0 <= float(confidence) <= 1
        ):
            return ()
        canonical = repr(sorted((str(key), value[key]) for key in value))
        validated.append(
            FindingCandidate(
                kind=FindingKind(value["kind"]),
                origin=FindingOrigin.MODEL,
                title=title.strip()[:200],
                normalized_value=_optional_string(value.get("normalized_value")),
                source_text=quoted,
                confidence=float(confidence),
                model_name=model_name,
                model_output_hash=hashlib.sha256(canonical.encode()).hexdigest(),
            )
        )
    return tuple(validated)


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)[:500]
