"""Bounded HTTP adapter for the actual pdf-inspector task API (no browser credentials)."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from uuid import uuid4

from contractops.errors import ContractOpsError
from contractops.infrastructure.document_parsing import ParsedBlock


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class PDFInspectorClient:
    def __init__(self, endpoint: str, api_key: str | None, profile: str, timeout: float) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Inspector URL must be a trusted HTTP(S) origin without credentials")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("non-loopback Inspector requires HTTPS")
        self.endpoint, self.api_key = endpoint.rstrip("/"), api_key
        self.profile, self.timeout = profile, timeout
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", profile):
            raise ValueError("Inspector profile id is invalid")
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _request(
        self, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        request_headers = {"Accept": "application/json", **(headers or {})}
        if self.api_key:
            request_headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(self.endpoint + path, data=body, headers=request_headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError("oversized result")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError("invalid result")
                return result, {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as error:
            raise ContractOpsError(
                code="inspector_unavailable"
                if error.code >= 500 or error.code in {408, 429}
                else "inspector_request_rejected",
                message="Inspector 请求失败，请检查服务状态、认证和模板配置。",
                status_code=502,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ContractOpsError(
                code="inspector_unavailable",
                message="Inspector 暂不可用；任务已保留，可同步恢复。",
                status_code=503,
            ) from None
        except (ValueError, UnicodeError):
            raise ContractOpsError(
                code="inspector_invalid_result",
                message="Inspector 返回格式或大小不合法。",
                status_code=502,
            ) from None

    def submit(self, content: bytes, request_key: str, profile: str) -> dict[str, Any]:
        boundary = "contractops-" + uuid4().hex
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="profile_id"\r\n\r\n'
                f'{profile}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                'filename="document.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        task, headers = self._request(
            "/v1/tasks",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Idempotency-Key": request_key,
            },
        )
        if not headers.get("x-operation-id"):
            raise ContractOpsError(
                code="inspector_protocol_unsupported",
                message="Inspector 未确认幂等提交，请升级服务并人工核对外部任务。",
                status_code=502,
            )
        self._task_id(task.get("id"))
        return task

    @staticmethod
    def _task_id(value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
            raise ContractOpsError(
                code="inspector_invalid_result", message="Inspector 任务 ID 不合法。"
            )
        return value

    def task(self, task_id: str) -> dict[str, Any]:
        return self._request("/v1/tasks/" + self._task_id(task_id))[0]

    def result(self, task_id: str) -> dict[str, Any]:
        return self._request("/v1/tasks/" + self._task_id(task_id) + "/result")[0]

    def preview(self, task_id: str, page: int) -> bytes:
        headers = {"Accept": "image/png"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(
            self.endpoint + f"/v1/tasks/{self._task_id(task_id)}/source/pages/{page}.png",
            headers=headers,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = response.read(8 * 1024 * 1024 + 1)
                if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 8 * 1024 * 1024:
                    raise ValueError("invalid image")
                return bytes(data)
        except (OSError, ValueError):
            raise ContractOpsError(
                code="inspector_preview_failed",
                message="原件页预览失败；请检查页码或稍后重试。",
                status_code=502,
            ) from None


def map_inspector_result(result: dict[str, Any]) -> tuple[ParsedBlock, ...]:
    """Keep every source page and table block; never collapse merged evidence to one page."""
    items = result.get("chunks")
    if not isinstance(items, list) or len(items) > 2000:
        raise ContractOpsError(
            code="inspector_invalid_result", message="Inspector 片段列表不合法。"
        )
    blocks: list[ParsedBlock] = []
    characters = 0
    for item in items:
        if not isinstance(item, dict):
            raise ContractOpsError(
                code="inspector_invalid_result", message="Inspector 片段不合法。"
            )
        content = item.get("markdown")
        pages, path = item.get("pages"), item.get("section_path", [])
        if (
            not isinstance(content, str)
            or not content.strip()
            or not isinstance(pages, list)
            or not pages
            or any(type(page) is not int or not 1 <= page <= 2000 for page in pages)
            or not isinstance(path, list)
            or any(not isinstance(title, str) for title in path)
        ):
            raise ContractOpsError(
                code="inspector_invalid_result", message="Inspector 片段来源不合法。"
            )
        if item.get("content_hash") != hashlib.sha256(content.encode()).hexdigest():
            raise ContractOpsError(
                code="inspector_hash_mismatch", message="Inspector 片段摘要校验失败。"
            )
        characters += len(content)
        if characters > 500_000:
            raise ContractOpsError(
                code="document_text_limit", message="Inspector 正文超过50万字符。"
            )
        source_id = item.get("id")
        kind = item.get("kind", "text")
        if (
            not isinstance(source_id, str)
            or len(source_id) > 100
            or kind not in {"text", "table", "heading"}
        ):
            raise ContractOpsError(
                code="inspector_invalid_result", message="Inspector 片段类型不合法。"
            )
        blocks.append(
            ParsedBlock(
                len(blocks),
                min(pages),
                tuple(path),
                content,
                tuple(sorted(set(pages))),
                source_id,
                kind,
            )
        )
    return tuple(blocks)
