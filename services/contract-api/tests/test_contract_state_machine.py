import pytest

from contractops.domain.contract import (
    ContractStatus,
    InvalidContractTransition,
    allowed_contract_transitions,
    transition_contract,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ContractStatus.DRAFT, ContractStatus.SUBMITTED),
        (ContractStatus.SUBMITTED, ContractStatus.IN_APPROVAL),
        (ContractStatus.IN_APPROVAL, ContractStatus.APPROVED),
        (ContractStatus.APPROVED, ContractStatus.ACTIVE),
        (ContractStatus.ACTIVE, ContractStatus.SUSPENDED),
        (ContractStatus.SUSPENDED, ContractStatus.ACTIVE),
        (ContractStatus.ACTIVE, ContractStatus.TERMINATED),
    ],
)
def test_contract_happy_path(current: ContractStatus, target: ContractStatus) -> None:
    assert transition_contract(current, target) is target


def test_active_contract_cannot_return_to_draft() -> None:
    with pytest.raises(InvalidContractTransition):
        transition_contract(ContractStatus.ACTIVE, ContractStatus.DRAFT)


def test_terminal_contract_states_have_no_outgoing_transitions() -> None:
    assert allowed_contract_transitions(ContractStatus.TERMINATED) == frozenset()
    assert allowed_contract_transitions(ContractStatus.EXPIRED) == frozenset()
