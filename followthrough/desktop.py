"""Local-first desktop control for Followthrough.

The local-vs-remote routing and visual no-op verification pattern is adapted
from the MIT-licensed nickvasilescu/nicks-stack orgo-desktop-local plugin.
This implementation is asynchronous, keeps durable receipts in Followthrough,
and intentionally excludes arbitrary bash/exec from the public desktop API.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from PIL import Image

from .config import Settings
from .store import Store


class DesktopError(RuntimeError):
    pass


class DesktopUnavailable(DesktopError):
    pass


def _image_bytes(payload: dict[str, Any]) -> bytes:
    encoded = payload.get("image") or payload.get("screenshot")
    if not isinstance(encoded, str) or not encoded:
        raise DesktopUnavailable("desktop screenshot response has no image")
    if encoded.startswith("data:"):
        encoded = encoded.partition(",")[2]
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise DesktopUnavailable("desktop screenshot is not valid base64") from exc


def frame_fingerprint(png: bytes, *, top_skip: int = 28) -> str:
    """Hash stable RGB pixels while cropping the desktop clock/panel."""
    with Image.open(io.BytesIO(png)) as source:
        image = source.convert("RGB")
        width, height = image.size
        if height > top_skip:
            image = image.crop((0, top_skip, width, height))
        return hashlib.sha256(image.tobytes()).hexdigest()


@dataclass(frozen=True)
class DesktopTarget:
    provider: str
    computer_id: str | None
    base_url: str
    token: str


class DesktopRouter:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.transport = transport

    @property
    def local_token(self) -> str:
        return self.settings.desktop_api_token.strip()

    async def _local_health(self) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=1.5, transport=self.transport) as client:
                response = await client.get(self.settings.desktop_api_base.rstrip("/") + "/health")
                response.raise_for_status()
                value = response.json()
                return value if isinstance(value, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def target(self, computer_id: str | None = None) -> DesktopTarget:
        if computer_id:
            if not self.settings.orgo_api_key:
                raise DesktopUnavailable("ORGO_API_KEY is required for a remote computer")
            return DesktopTarget(
                "orgo-remote",
                computer_id,
                self.settings.orgo_api_base.rstrip("/"),
                self.settings.orgo_api_key,
            )
        health = await self._local_health()
        if health and self.local_token:
            return DesktopTarget(
                "spark-local", None, self.settings.desktop_api_base.rstrip("/"), self.local_token
            )
        if self.settings.orgo_api_key and self.settings.orgo_default_computer_id:
            return DesktopTarget(
                "orgo-remote",
                self.settings.orgo_default_computer_id,
                self.settings.orgo_api_base.rstrip("/"),
                self.settings.orgo_api_key,
            )
        if health and not self.local_token:
            raise DesktopUnavailable("Spark Desktop API is present but its token is missing")
        raise DesktopUnavailable(
            "no desktop is configured; set FOLLOWTHROUGH_DESKTOP_API_TOKEN or configure the optional Orgo fallback"
        )

    @staticmethod
    def _path(target: DesktopTarget, action: str) -> str:
        if target.provider == "spark-local":
            return f"/{action}"
        return f"/computers/{target.computer_id}/{action}"

    @staticmethod
    def _headers(target: DesktopTarget) -> dict[str, str]:
        return {"Authorization": f"Bearer {target.token}", "Accept": "application/json"}

    async def _request(
        self,
        target: DesktopTarget,
        method: str,
        action: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = max(5, self.settings.desktop_action_timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=target.base_url,
                headers=self._headers(target),
                timeout=timeout,
                transport=self.transport,
            ) as client:
                response = await client.request(method, self._path(target, action), json=body)
                response.raise_for_status()
                if not response.content:
                    return {"ok": True, "status_code": response.status_code}
                value = response.json()
                return value if isinstance(value, dict) else {"result": value}
        except (httpx.HTTPError, ValueError) as exc:
            raise DesktopError(
                f"{target.provider} {action} failed: {type(exc).__name__}"
            ) from exc

    async def screenshot(self, computer_id: str | None = None) -> tuple[bytes, dict[str, Any]]:
        target = await self.target(computer_id)
        payload = await self._request(target, "GET", "screenshot")
        png = _image_bytes(payload)
        with Image.open(io.BytesIO(png)) as image:
            width, height = image.size
        return png, {
            "provider": target.provider,
            "computer_id": target.computer_id,
            "width": width,
            "height": height,
            "fingerprint": frame_fingerprint(png),
        }

    async def doctor(self, computer_id: str | None = None) -> dict[str, Any]:
        local_health = await self._local_health()
        report: dict[str, Any] = {
            "ready": False,
            "local": {
                "present": bool(local_health),
                "token_present": bool(self.local_token),
                "health": local_health,
            },
            "remote": {
                "key_present": bool(self.settings.orgo_api_key),
                "default_computer_id": computer_id or self.settings.orgo_default_computer_id or None,
            },
            "prefer": "unavailable",
            "errors": [],
        }
        try:
            target = await self.target(computer_id)
            png, shot = await self.screenshot(target.computer_id if target.provider == "orgo-remote" else None)
            report.update(
                {
                    "ready": bool(png),
                    "prefer": target.provider,
                    "provider": target.provider,
                    "computer_id": target.computer_id,
                    "screenshot": shot,
                }
            )
        except Exception as exc:
            report["errors"].append(f"{type(exc).__name__}: {str(exc)[:400]}")
        return report

    async def _verified_action(
        self,
        action: str,
        body: dict[str, Any],
        *,
        computer_id: str | None,
        verify: bool,
    ) -> dict[str, Any]:
        target = await self.target(computer_id)
        before = None
        if verify:
            before_png, _ = await self.screenshot(target.computer_id if target.provider == "orgo-remote" else None)
            before = frame_fingerprint(before_png)
        result = await self._request(target, "POST", action, body)
        after = None
        if verify:
            await asyncio.sleep(0.3)
            after_png, _ = await self.screenshot(target.computer_id if target.provider == "orgo-remote" else None)
            after = frame_fingerprint(after_png)
        changed = None if before is None or after is None else before != after
        receipt = {
            "id": str(uuid.uuid4()),
            "provider": target.provider,
            "computer_id": target.computer_id,
            "action": action,
            "visual_changed": changed,
            "noop": None if changed is None else not changed,
            "fingerprint_before": before,
            "fingerprint_after": after,
            "result": result,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.store.record_desktop_action(receipt)
        return receipt

    async def click(
        self, x: int, y: int, *, button: str = "left", double: bool = False,
        computer_id: str | None = None, verify: bool = True,
    ) -> dict[str, Any]:
        return await self._verified_action(
            "click", {"x": x, "y": y, "button": button, "double": double},
            computer_id=computer_id, verify=verify,
        )

    async def drag(
        self, start_x: int, start_y: int, end_x: int, end_y: int, *,
        computer_id: str | None = None, verify: bool = True,
    ) -> dict[str, Any]:
        return await self._verified_action(
            "drag",
            {"start_x": start_x, "start_y": start_y, "end_x": end_x, "end_y": end_y},
            computer_id=computer_id, verify=verify,
        )

    async def type_text(
        self, text: str, *, delay_ms: int = 12, computer_id: str | None = None,
        verify: bool = True,
    ) -> dict[str, Any]:
        return await self._verified_action(
            "type", {"text": text, "delay_ms": delay_ms}, computer_id=computer_id, verify=verify
        )

    async def key(
        self, key: str, *, computer_id: str | None = None, verify: bool = True
    ) -> dict[str, Any]:
        return await self._verified_action(
            "key", {"key": key}, computer_id=computer_id, verify=verify
        )

    async def scroll(
        self, direction: str, *, amount: int = 3, x: int = 640, y: int = 360,
        computer_id: str | None = None, verify: bool = True,
    ) -> dict[str, Any]:
        return await self._verified_action(
            "scroll", {"direction": direction, "amount": amount, "x": x, "y": y},
            computer_id=computer_id, verify=verify,
        )

    async def lifecycle(
        self, operation: str, *, computer_id: str | None = None, confirmed: bool = False
    ) -> dict[str, Any]:
        if operation in {"stop", "restart"} and not confirmed:
            raise ValueError(f"{operation} requires confirmed=true")
        target = await self.target(computer_id)
        action = "ensure-running" if operation == "ensure_running" else operation
        result = await self._request(target, "POST", action)
        receipt = {
            "id": str(uuid.uuid4()), "provider": target.provider,
            "computer_id": target.computer_id, "action": f"lifecycle.{operation}",
            "visual_changed": None, "noop": None, "fingerprint_before": None,
            "fingerprint_after": None, "result": result,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.store.record_desktop_action(receipt)
        return receipt
