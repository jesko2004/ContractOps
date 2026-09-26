from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from contractops.context import ActorContext, DataScope, Role
from contractops.domain.contract import Contract, ContractVersion
from contractops.errors import ContractOpsError


@dataclass(frozen=True, slots=True)
class CreateContractCommand:
    contract_number: str | None
    title: str
    contract_type: str
    counterparty_name: str | None
    department_id: UUID
    amount: Decimal | None
    currency: str | None
    valid_from: date | None
    valid_until: date | None


@dataclass(frozen=True, slots=True)
class AddContractVersionCommand:
    file_name: str
    media_type: str
    size_bytes: int
    content_hash: str


class ContractLedger(Protocol):
    def create_contract(
        self,
        actor: ActorContext,
        command: CreateContractCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Contract, bool]: ...

    def get_contract(self, actor: ActorContext, contract_id: UUID) -> Contract | None: ...

    def add_version(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: AddContractVersionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ContractVersion, bool]: ...


def _request_hash(value: CreateContractCommand | AddContractVersionCommand) -> str:
    serialized = json.dumps(
        asdict(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _forbidden(message: str) -> ContractOpsError:
    return ContractOpsError(code="authorization_denied", message=message, status_code=403)


class ContractService:
    _WRITER_ROLES = (Role.CONTRACT_OWNER, Role.TENANT_ADMIN)
    _READER_ROLES = (
        Role.CONTRACT_OWNER,
        Role.APPROVER,
        Role.LEGAL_ADMIN,
        Role.FINANCE_APPROVER,
        Role.BUSINESS_APPROVER,
        Role.AUDITOR,
        Role.TENANT_ADMIN,
    )

    def __init__(self, ledger: ContractLedger) -> None:
        self._ledger = ledger

    def create_contract(
        self,
        actor: ActorContext,
        command: CreateContractCommand,
        *,
        idempotency_key: str,
    ) -> tuple[Contract, bool]:
        if not actor.has_any_role(*self._WRITER_ROLES):
            raise _forbidden("the caller cannot create contracts")
        if not actor.can_access_department(command.department_id):
            raise _forbidden("the caller cannot create contracts for this department")
        if (command.currency is None) != (command.amount is None):
            raise ContractOpsError(
                code="contract_amount_currency_required",
                message="amount and currency must be provided together",
            )
        if (
            command.valid_from is not None
            and command.valid_until is not None
            and command.valid_until < command.valid_from
        ):
            raise ContractOpsError(
                code="contract_validity_invalid",
                message="valid_until cannot be earlier than valid_from",
            )
        return self._ledger.create_contract(
            actor,
            command,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(command),
        )

    def get_contract(self, actor: ActorContext, contract_id: UUID) -> Contract:
        if not actor.has_any_role(*self._READER_ROLES):
            raise _forbidden("the caller cannot read contracts")
        contract = self._ledger.get_contract(actor, contract_id)
        if contract is None:
            raise ContractOpsError(
                code="contract_not_found",
                message="contract was not found",
                status_code=404,
            )
        if not self._can_read(actor, contract):
            raise _forbidden("the caller cannot read this contract")
        return contract

    def add_version(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: AddContractVersionCommand,
        *,
        idempotency_key: str,
    ) -> tuple[ContractVersion, bool]:
        if not actor.has_any_role(*self._WRITER_ROLES):
            raise _forbidden("the caller cannot add contract versions")
        contract = self.get_contract(actor, contract_id)
        if actor.data_scope is DataScope.OWN and contract.created_by != actor.user_id:
            raise _forbidden("the caller cannot modify this contract")
        return self._ledger.add_version(
            actor,
            contract_id,
            command,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(command),
        )

    @staticmethod
    def _can_read(actor: ActorContext, contract: Contract) -> bool:
        if actor.data_scope is DataScope.TENANT:
            return True
        if contract.created_by == actor.user_id:
            return True
        return (
            actor.data_scope is DataScope.DEPARTMENT
            and contract.department_id in actor.department_ids
        )
