import pytest

from contractops.domain.review import (
    InvalidReviewTransition,
    ReviewStatus,
    allowed_review_transitions,
    transition_review,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ReviewStatus.DRAFT, ReviewStatus.INGESTING),
        (ReviewStatus.INGESTING, ReviewStatus.AI_REVIEW),
        (ReviewStatus.AI_REVIEW, ReviewStatus.HUMAN_REVIEW),
        (ReviewStatus.HUMAN_REVIEW, ReviewStatus.APPROVED),
        (ReviewStatus.APPROVED, ReviewStatus.ACTIVE),
        (ReviewStatus.ACTIVE, ReviewStatus.EXPIRED),
    ],
)
def test_happy_path_transitions(current: ReviewStatus, target: ReviewStatus) -> None:
    assert transition_review(current, target) is target


def test_active_review_cannot_return_to_draft() -> None:
    with pytest.raises(InvalidReviewTransition):
        transition_review(ReviewStatus.ACTIVE, ReviewStatus.DRAFT)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert allowed_review_transitions(ReviewStatus.CANCELLED) == frozenset()
    assert allowed_review_transitions(ReviewStatus.EXPIRED) == frozenset()
