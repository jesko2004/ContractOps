from enum import StrEnum


class ContractStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    IN_APPROVAL = "IN_APPROVAL"
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REJECTED = "REJECTED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    EXPIRED = "EXPIRED"


class InvalidContractTransition(ValueError):
    def __init__(self, current: ContractStatus, target: ContractStatus) -> None:
        super().__init__(f"contract transition {current.value} -> {target.value} is not allowed")
        self.current = current
        self.target = target


_ALLOWED_TRANSITIONS: dict[ContractStatus, frozenset[ContractStatus]] = {
    ContractStatus.DRAFT: frozenset({ContractStatus.SUBMITTED}),
    ContractStatus.SUBMITTED: frozenset({ContractStatus.IN_APPROVAL}),
    ContractStatus.IN_APPROVAL: frozenset(
        {
            ContractStatus.APPROVED,
            ContractStatus.CHANGES_REQUESTED,
            ContractStatus.REJECTED,
        }
    ),
    ContractStatus.CHANGES_REQUESTED: frozenset({ContractStatus.DRAFT}),
    ContractStatus.REJECTED: frozenset({ContractStatus.DRAFT}),
    ContractStatus.APPROVED: frozenset({ContractStatus.ACTIVE}),
    ContractStatus.ACTIVE: frozenset(
        {
            ContractStatus.SUSPENDED,
            ContractStatus.TERMINATED,
            ContractStatus.EXPIRED,
        }
    ),
    ContractStatus.SUSPENDED: frozenset({ContractStatus.ACTIVE}),
    ContractStatus.TERMINATED: frozenset(),
    ContractStatus.EXPIRED: frozenset(),
}


def transition_contract(current: ContractStatus, target: ContractStatus) -> ContractStatus:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidContractTransition(current=current, target=target)
    return target


def allowed_contract_transitions(current: ContractStatus) -> frozenset[ContractStatus]:
    return _ALLOWED_TRANSITIONS[current]
