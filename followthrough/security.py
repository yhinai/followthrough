from __future__ import annotations

import hmac
from pathlib import Path


class AuthenticationError(ValueError):
    pass


def bearer_token(authorization: str | None) -> str:
    if authorization:
        scheme, separator, value = authorization.partition(" ")
        if separator and scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return ""


class TokenAuthority:
    def __init__(
        self, dashboard_token_file: Path, device_tokens_dir: Path, required: bool = True
    ) -> None:
        self.dashboard_token_file = dashboard_token_file
        self.device_tokens_dir = device_tokens_dir
        self.required = required

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    def _device_tokens(self) -> list[str]:
        if not self.device_tokens_dir.is_dir():
            return []
        return [
            token
            for path in sorted(self.device_tokens_dir.glob("*.token"))
            if (token := self._read(path))
        ]

    def dashboard(self, candidate: str) -> bool:
        if not self.required:
            return True
        expected = self._read(self.dashboard_token_file)
        return bool(expected and candidate and hmac.compare_digest(candidate, expected))

    def device(self, candidate: str) -> bool:
        if not self.required:
            return True
        tokens = self._device_tokens()
        return bool(
            candidate and any(hmac.compare_digest(candidate, expected) for expected in tokens)
        )

    def dashboard_or_device(self, candidate: str) -> bool:
        return self.dashboard(candidate) or self.device(candidate)

    def ready(self) -> bool:
        return bool(self._read(self.dashboard_token_file) and self._device_tokens())
