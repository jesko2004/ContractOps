from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from contractops.config import Settings
from contractops.context import ActorContext, DataScope, Role
from contractops.errors import ContractOpsError


def _unauthorized(message: str = "authentication credentials are invalid") -> ContractOpsError:
    return ContractOpsError(code="authentication_invalid", message=message, status_code=401)


def _decode_segment(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, binascii.Error) as exc:
        raise _unauthorized() from exc


def _json_object(value: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _unauthorized() from exc
    if not isinstance(decoded, dict):
        raise _unauthorized()
    return decoded


def _required_string(claims: Mapping[str, Any], key: str) -> str:
    value = claims.get(key)
    if not isinstance(value, str) or not value:
        raise _unauthorized()
    return value


def _required_timestamp(claims: Mapping[str, Any], key: str) -> float:
    value = claims.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise _unauthorized()
    return float(value)


class JWTDecoder:
    """Minimal strict HS256 decoder for the service's first-party access tokens."""

    def __init__(self, settings: Settings) -> None:
        self._secret = settings.jwt_secret.encode("utf-8")
        self._issuer = settings.jwt_issuer
        self._audience = settings.jwt_audience
        self._leeway = settings.jwt_leeway_seconds

    def decode(self, token: str, *, now: float | None = None) -> ActorContext:
        parts = token.split(".")
        if len(parts) != 3:
            raise _unauthorized()
        encoded_header, encoded_payload, encoded_signature = parts
        header = _json_object(_decode_segment(encoded_header))
        if header.get("alg") != "HS256" or header.get("typ") not in (None, "JWT"):
            raise _unauthorized()

        signed = f"{encoded_header}.{encoded_payload}".encode("ascii")
        expected = hmac.new(self._secret, signed, hashlib.sha256).digest()
        signature = _decode_segment(encoded_signature)
        if not hmac.compare_digest(expected, signature):
            raise _unauthorized()

        claims = _json_object(_decode_segment(encoded_payload))
        current_time = time.time() if now is None else now
        expires_at = _required_timestamp(claims, "exp")
        if expires_at < current_time - self._leeway:
            raise _unauthorized("authentication credentials have expired")
        not_before = claims.get("nbf")
        if not_before is not None:
            if not isinstance(not_before, int | float) or isinstance(not_before, bool):
                raise _unauthorized()
            if float(not_before) > current_time + self._leeway:
                raise _unauthorized()
        if _required_string(claims, "iss") != self._issuer:
            raise _unauthorized()
        audience = claims.get("aud")
        audience_matches = audience == self._audience or (
            isinstance(audience, list) and self._audience in audience
        )
        if not audience_matches:
            raise _unauthorized()

        try:
            tenant_id = UUID(_required_string(claims, "tenant_id"))
            user_id = UUID(_required_string(claims, "sub"))
            roles_value = claims.get("roles")
            departments_value = claims.get("department_ids", [])
            if not isinstance(roles_value, list) or not all(
                isinstance(value, str) for value in roles_value
            ):
                raise ValueError("roles must be a string list")
            if not isinstance(departments_value, list) or not all(
                isinstance(value, str) for value in departments_value
            ):
                raise ValueError("department_ids must be a string list")
            roles = frozenset(Role(value) for value in roles_value)
            departments = frozenset(UUID(value) for value in departments_value)
            data_scope = DataScope(_required_string(claims, "data_scope"))
        except (ValueError, TypeError) as exc:
            raise _unauthorized() from exc
        if not roles:
            raise _unauthorized()
        return ActorContext(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles,
            department_ids=departments,
            data_scope=data_scope,
        )
