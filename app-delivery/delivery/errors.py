from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeliveryError(Exception):
    code: str
    message: str
    exit_code: int = 1
    details: dict[str, Any] = field(default_factory=dict)
    suggested_action: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "error",
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        if self.suggested_action:
            payload["suggested_action"] = self.suggested_action
        return payload


def error_payload(exc: Exception) -> tuple[dict[str, Any], int]:
    if isinstance(exc, DeliveryError):
        return exc.to_payload(), exc.exit_code
    details: dict[str, Any] = {}
    output = getattr(exc, "output", "")
    if isinstance(output, str) and output.strip():
        details["output"] = output.strip()[:4000]
    return (
        {
            "status": "error",
            "code": exc.__class__.__name__,
            "message": str(exc),
            **({"details": details} if details else {}),
        },
        1,
    )