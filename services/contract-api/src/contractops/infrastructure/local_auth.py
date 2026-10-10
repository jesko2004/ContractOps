import hmac

from contractops.auth import JWTDecoder
from contractops.context import ActorContext
from contractops.errors import ContractOpsError


class LocalTokenDecoder(JWTDecoder):
    """Explicit local-only identity; never used by the default/production factory."""

    def __init__(
        self, token: str, actor: ActorContext, alternatives: dict[str, ActorContext] | None = None
    ) -> None:
        self.token = token
        self.actor = actor
        self.alternatives = alternatives or {}

    def decode(self, token: str, *, now: float | None = None) -> ActorContext:
        for candidate, actor in self.alternatives.items():
            if hmac.compare_digest(token.encode("utf-8"), candidate.encode("utf-8")):
                return actor
        if not hmac.compare_digest(token.encode("utf-8"), self.token.encode("utf-8")):
            raise ContractOpsError(
                code="authentication_invalid",
                message="本地访问令牌无效，请使用当前启动日志中的令牌。",
                status_code=401,
            )
        return self.actor
