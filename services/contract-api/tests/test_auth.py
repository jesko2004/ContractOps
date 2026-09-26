import base64
import hashlib
import hmac
import json
from typing import Any
from uuid import uuid4

import pytest

from contractops.auth import JWTDecoder
from contractops.config import Settings
from contractops.context import DataScope, Role
from contractops.errors import ContractOpsError


def _segment(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _token(payload: dict[str, Any], secret: str = "test-secret") -> str:
    header = _segment({"alg": "HS256", "typ": "JWT"})
    body = _segment(payload)
    signature = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{header}.{body}.{encoded_signature}"


def _claims() -> dict[str, Any]:
    return {
        "iss": "contractops-test",
        "aud": "contractops-api-test",
        "sub": str(uuid4()),
        "tenant_id": str(uuid4()),
        "roles": ["CONTRACT_OWNER"],
        "department_ids": [str(uuid4())],
        "data_scope": "DEPARTMENT",
        "exp": 2_000,
    }


def _decoder() -> JWTDecoder:
    return JWTDecoder(
        Settings(
            jwt_secret="test-secret",
            jwt_issuer="contractops-test",
            jwt_audience="contractops-api-test",
            jwt_leeway_seconds=0,
        )
    )


def test_decoder_builds_server_derived_actor_context() -> None:
    claims = _claims()
    actor = _decoder().decode(_token(claims), now=1_000)

    assert str(actor.tenant_id) == claims["tenant_id"]
    assert str(actor.user_id) == claims["sub"]
    assert actor.roles == frozenset({Role.CONTRACT_OWNER})
    assert actor.data_scope is DataScope.DEPARTMENT
    assert {str(value) for value in actor.department_ids} == set(claims["department_ids"])


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda claims: claims.update(exp=999), "expired"),
        (lambda claims: claims.update(iss="another-service"), "invalid"),
        (lambda claims: claims.update(aud="another-api"), "invalid"),
        (lambda claims: claims.update(roles=["UNKNOWN"]), "invalid"),
    ],
)
def test_decoder_rejects_invalid_claims(mutation: Any, message: str) -> None:
    claims = _claims()
    mutation(claims)

    with pytest.raises(ContractOpsError, match=message):
        _decoder().decode(_token(claims), now=1_000)


def test_decoder_rejects_tampered_signature() -> None:
    token = _token(_claims())
    header, payload, signature = token.split(".")
    tampered_signature = f"{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    tampered = f"{header}.{payload}.{tampered_signature}"

    with pytest.raises(ContractOpsError) as error:
        _decoder().decode(tampered, now=1_000)

    assert error.value.code == "authentication_invalid"
