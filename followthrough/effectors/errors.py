from __future__ import annotations

from typing import Any


class EffectorError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        uncertain: bool = False,
        retry_after_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.uncertain = uncertain
        self.retry_after_seconds = retry_after_seconds
        self.metadata = metadata or {}

    def safe_details(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "retryable": self.retryable,
            "uncertain": self.uncertain,
            "retry_after_seconds": self.retry_after_seconds,
            "metadata": self.metadata,
        }


class IdempotencyConflict(EffectorError):
    def __init__(self) -> None:
        super().__init__(
            "idempotency key is already bound to a different request",
            code="idempotency_conflict",
        )


class InvalidTransition(EffectorError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(
            f"effect cannot transition from {current} to {target}",
            code="invalid_transition",
            metadata={"current": current, "target": target},
        )


class PolicyDenied(EffectorError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(
            "action is denied by the typed autonomy policy",
            code="policy_denied",
            metadata={"reason_code": reason_code},
        )


class ApprovalRequired(EffectorError):
    def __init__(self) -> None:
        super().__init__(
            "action requires explicit approval",
            code="approval_required",
        )


class AuthenticationExpired(EffectorError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            "connector authentication is missing or expired",
            code="authentication_expired",
            retryable=True,
            metadata={"provider": provider},
        )


class RateLimited(EffectorError):
    def __init__(self, provider: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(
            "connector rate limit reached",
            code="rate_limited",
            retryable=True,
            retry_after_seconds=retry_after_seconds,
            metadata={"provider": provider},
        )


class PartialFailure(EffectorError):
    def __init__(self, provider: str, operation: str) -> None:
        super().__init__(
            "connector outcome is uncertain and must be reconciled before retry",
            code="partial_failure",
            uncertain=True,
            metadata={"provider": provider, "operation": operation},
        )


class ConnectorFailure(EffectorError):
    def __init__(self, provider: str, operation: str, *, retryable: bool = False) -> None:
        super().__init__(
            "connector operation failed",
            code="connector_failure",
            retryable=retryable,
            metadata={"provider": provider, "operation": operation},
        )


class RollbackUnavailable(EffectorError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            "connector does not provide a safe typed rollback for this receipt",
            code="rollback_unavailable",
            metadata={"provider": provider},
        )
