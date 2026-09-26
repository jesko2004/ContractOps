from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import PurePath
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping

from contractops.application.contracts import (
    AddContractVersionCommand,
    ContractLedger,
    CreateContractCommand,
)
from contractops.context import ActorContext
from contractops.domain.contract import (
    Contract,
    ContractStatus,
    ContractVersion,
    ContractVersionStatus,
)
from contractops.errors import ContractOpsError
from contractops.infrastructure.postgres.database import Database

_SAFE_FILE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_file_name(value: str) -> str:
    name = PurePath(value.replace("\\", "/")).name
    sanitized = _SAFE_FILE_NAME.sub("-", name).strip(".-")
    return sanitized[:180] or "contract-document"


class PostgresContractLedger(ContractLedger):
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_contract(
        self,
        actor: ActorContext,
        command: CreateContractCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Contract, bool]:
        operation = "contract.create"
        with self._database.transaction(actor) as connection:
            self._lock_idempotency(connection, actor, operation, idempotency_key)
            replay_id = self._replay_resource_id(
                connection,
                operation,
                idempotency_key,
                request_hash,
            )
            if replay_id is not None:
                replay = self._load_contract(connection, replay_id)
                if replay is None:
                    raise RuntimeError("idempotency record references a missing contract")
                return replay, True

            contract_id = uuid4()
            inserted = cast(
                UUID | None,
                connection.execute(
                    text(
                        """
                    INSERT INTO contracts (
                        id, tenant_id, contract_number, title, contract_type,
                        counterparty_name, department_id, amount, currency,
                        valid_from, valid_until, created_by
                    ) VALUES (
                        :id, :tenant_id, :contract_number, :title, :contract_type,
                        :counterparty_name, :department_id, :amount, :currency,
                        :valid_from, :valid_until, :created_by
                    )
                    ON CONFLICT (tenant_id, contract_number) DO NOTHING
                    RETURNING id
                    """
                    ),
                    {
                        "id": contract_id,
                        "tenant_id": actor.tenant_id,
                        "contract_number": command.contract_number,
                        "title": command.title,
                        "contract_type": command.contract_type,
                        "counterparty_name": command.counterparty_name,
                        "department_id": command.department_id,
                        "amount": command.amount,
                        "currency": command.currency,
                        "valid_from": command.valid_from,
                        "valid_until": command.valid_until,
                        "created_by": actor.user_id,
                    },
                ).scalar_one_or_none(),
            )
            if inserted is None:
                raise ContractOpsError(
                    code="contract_number_conflict",
                    message="contract_number already exists in this tenant",
                    status_code=409,
                )
            self._record_idempotency(
                connection,
                actor,
                operation,
                idempotency_key,
                request_hash,
                contract_id,
            )
            created = self._load_contract(connection, contract_id)
            if created is None:
                raise RuntimeError("created contract could not be reloaded")
            return created, False

    def get_contract(self, actor: ActorContext, contract_id: UUID) -> Contract | None:
        with self._database.transaction(actor) as connection:
            return self._load_contract(connection, contract_id)

    def add_version(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: AddContractVersionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ContractVersion, bool]:
        operation = f"contract.version.create:{contract_id}"
        with self._database.transaction(actor) as connection:
            self._lock_idempotency(connection, actor, operation, idempotency_key)
            replay_id = self._replay_resource_id(
                connection,
                operation,
                idempotency_key,
                request_hash,
            )
            if replay_id is not None:
                replay = self._load_version(connection, replay_id)
                if replay is None:
                    raise RuntimeError("idempotency record references a missing contract version")
                return replay, True

            locked_contract = (
                connection.execute(
                    text("SELECT id, current_version_id FROM contracts WHERE id = :id FOR UPDATE"),
                    {"id": contract_id},
                )
                .mappings()
                .one_or_none()
            )
            if locked_contract is None:
                raise ContractOpsError(
                    code="contract_not_found",
                    message="contract was not found",
                    status_code=404,
                )
            duplicate = connection.execute(
                text(
                    """
                    SELECT id FROM contract_versions
                    WHERE contract_id = :contract_id AND content_hash = :content_hash
                    """
                ),
                {"contract_id": contract_id, "content_hash": command.content_hash},
            ).scalar_one_or_none()
            if duplicate is not None:
                raise ContractOpsError(
                    code="contract_version_duplicate_content",
                    message="this file content already exists for the contract",
                    status_code=409,
                    details={"version_id": str(duplicate)},
                )
            next_version = cast(
                int,
                connection.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(version_number), 0) + 1
                        FROM contract_versions WHERE contract_id = :contract_id
                        """
                    ),
                    {"contract_id": contract_id},
                ).scalar_one(),
            )
            version_id = uuid4()
            object_key = (
                f"tenants/{actor.tenant_id}/contracts/{contract_id}/versions/"
                f"{version_id}/{_safe_file_name(command.file_name)}"
            )
            connection.execute(
                text(
                    """
                    INSERT INTO contract_versions (
                        id, tenant_id, contract_id, version_number, object_key,
                        file_name, media_type, size_bytes, content_hash, created_by
                    ) VALUES (
                        :id, :tenant_id, :contract_id, :version_number, :object_key,
                        :file_name, :media_type, :size_bytes, :content_hash, :created_by
                    )
                    """
                ),
                {
                    "id": version_id,
                    "tenant_id": actor.tenant_id,
                    "contract_id": contract_id,
                    "version_number": next_version,
                    "object_key": object_key,
                    "file_name": command.file_name,
                    "media_type": command.media_type,
                    "size_bytes": command.size_bytes,
                    "content_hash": command.content_hash,
                    "created_by": actor.user_id,
                },
            )
            if locked_contract["current_version_id"] is None:
                connection.execute(
                    text(
                        """
                        UPDATE contracts
                        SET current_version_id = :version_id, updated_at = now(),
                            state_version = state_version + 1
                        WHERE id = :contract_id
                        """
                    ),
                    {"version_id": version_id, "contract_id": contract_id},
                )
            self._record_idempotency(
                connection,
                actor,
                operation,
                idempotency_key,
                request_hash,
                version_id,
            )
            created = self._load_version(connection, version_id)
            if created is None:
                raise RuntimeError("created contract version could not be reloaded")
            return created, False

    @staticmethod
    def _lock_idempotency(
        connection: Connection,
        actor: ActorContext,
        operation: str,
        idempotency_key: str,
    ) -> None:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:value, 0))"),
            {"value": f"{actor.tenant_id}:{operation}:{idempotency_key}"},
        )

    @staticmethod
    def _replay_resource_id(
        connection: Connection,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> UUID | None:
        row = (
            connection.execute(
                text(
                    """
                SELECT request_hash, resource_id FROM idempotency_records
                WHERE operation = :operation AND idempotency_key = :idempotency_key
                """
                ),
                {"operation": operation, "idempotency_key": idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ContractOpsError(
                code="idempotency_key_reused",
                message="Idempotency-Key was already used with a different request",
                status_code=409,
            )
        return cast(UUID, row["resource_id"])

    @staticmethod
    def _record_idempotency(
        connection: Connection,
        actor: ActorContext,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        resource_id: UUID,
    ) -> None:
        response = json.dumps({"resource_id": str(resource_id)}, separators=(",", ":"))
        connection.execute(
            text(
                """
                INSERT INTO idempotency_records (
                    tenant_id, operation, idempotency_key, request_hash,
                    response_status, response_body, resource_id, expires_at
                ) VALUES (
                    :tenant_id, :operation, :idempotency_key, :request_hash,
                    201, CAST(:response_body AS jsonb), :resource_id, :expires_at
                )
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "operation": operation,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "response_body": response,
                "resource_id": resource_id,
                "expires_at": datetime.now(UTC) + timedelta(hours=24),
            },
        )

    def _load_contract(self, connection: Connection, contract_id: UUID) -> Contract | None:
        row = (
            connection.execute(
                text(
                    """
                SELECT id, tenant_id, contract_number, title, contract_type,
                       counterparty_name, department_id, amount, currency,
                       valid_from, valid_until, status, current_version_id,
                       state_version, created_by, created_at, updated_at
                FROM contracts WHERE id = :id
                """
                ),
                {"id": contract_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        versions = (
            connection.execute(
                text(
                    """
                SELECT id, contract_id, version_number, status, object_key,
                       file_name, media_type, size_bytes, content_hash,
                       created_by, created_at
                FROM contract_versions
                WHERE contract_id = :contract_id
                ORDER BY version_number
                """
                ),
                {"contract_id": contract_id},
            )
            .mappings()
            .all()
        )
        mapped_versions = tuple(self._version_from_row(item) for item in versions)
        return self._contract_from_row(row, mapped_versions)

    def _load_version(self, connection: Connection, version_id: UUID) -> ContractVersion | None:
        row = (
            connection.execute(
                text(
                    """
                SELECT id, contract_id, version_number, status, object_key,
                       file_name, media_type, size_bytes, content_hash,
                       created_by, created_at
                FROM contract_versions WHERE id = :id
                """
                ),
                {"id": version_id},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._version_from_row(row)

    @staticmethod
    def _contract_from_row(
        row: RowMapping,
        versions: tuple[ContractVersion, ...],
    ) -> Contract:
        return Contract(
            id=cast(UUID, row["id"]),
            tenant_id=cast(UUID, row["tenant_id"]),
            contract_number=cast(str | None, row["contract_number"]),
            title=cast(str, row["title"]),
            contract_type=cast(str, row["contract_type"]),
            counterparty_name=cast(str | None, row["counterparty_name"]),
            department_id=cast(UUID, row["department_id"]),
            amount=cast(Decimal | None, row["amount"]),
            currency=cast(str | None, row["currency"]),
            valid_from=cast(Any, row["valid_from"]),
            valid_until=cast(Any, row["valid_until"]),
            status=ContractStatus(cast(str, row["status"])),
            current_version_id=cast(UUID | None, row["current_version_id"]),
            state_version=cast(int, row["state_version"]),
            created_by=cast(UUID, row["created_by"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
            versions=versions,
        )

    @staticmethod
    def _version_from_row(row: RowMapping) -> ContractVersion:
        return ContractVersion(
            id=cast(UUID, row["id"]),
            contract_id=cast(UUID, row["contract_id"]),
            version_number=cast(int, row["version_number"]),
            status=ContractVersionStatus(cast(str, row["status"])),
            object_key=cast(str, row["object_key"]),
            file_name=cast(str, row["file_name"]),
            media_type=cast(str, row["media_type"]),
            size_bytes=cast(int, row["size_bytes"]),
            content_hash=cast(str, row["content_hash"]),
            created_by=cast(UUID, row["created_by"]),
            created_at=cast(datetime, row["created_at"]),
        )
