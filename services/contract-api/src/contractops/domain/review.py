from enum import StrEnum


class ReviewStatus(StrEnum):
    DRAFT = "DRAFT"
    INGESTING = "INGESTING"
    AI_REVIEW = "AI_REVIEW"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class InvalidReviewTransition(ValueError):
    def __init__(self, current: ReviewStatus, target: ReviewStatus) -> None:
        super().__init__(f"review transition {current.value} -> {target.value} is not allowed")
        self.current = current
        self.target = target


_ALLOWED_TRANSITIONS: dict[ReviewStatus, frozenset[ReviewStatus]] = {
    ReviewStatus.DRAFT: frozenset({ReviewStatus.INGESTING, ReviewStatus.CANCELLED}),
    ReviewStatus.INGESTING: frozenset(
        {ReviewStatus.AI_REVIEW, ReviewStatus.FAILED, ReviewStatus.CANCELLED}
    ),
    ReviewStatus.AI_REVIEW: frozenset(
        {ReviewStatus.HUMAN_REVIEW, ReviewStatus.FAILED, ReviewStatus.CANCELLED}
    ),
    ReviewStatus.HUMAN_REVIEW: frozenset(
        {ReviewStatus.APPROVED, ReviewStatus.REJECTED, ReviewStatus.CANCELLED}
    ),
    ReviewStatus.REJECTED: frozenset({ReviewStatus.DRAFT, ReviewStatus.CANCELLED}),
    ReviewStatus.FAILED: frozenset({ReviewStatus.INGESTING, ReviewStatus.CANCELLED}),
    ReviewStatus.APPROVED: frozenset({ReviewStatus.ACTIVE, ReviewStatus.CANCELLED}),
    ReviewStatus.ACTIVE: frozenset({ReviewStatus.EXPIRED}),
    ReviewStatus.CANCELLED: frozenset(),
    ReviewStatus.EXPIRED: frozenset(),
}


def transition_review(current: ReviewStatus, target: ReviewStatus) -> ReviewStatus:
    """Validate and return the target status for a review transition."""
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidReviewTransition(current=current, target=target)
    return target


def allowed_review_transitions(current: ReviewStatus) -> frozenset[ReviewStatus]:
    return _ALLOWED_TRANSITIONS[current]
