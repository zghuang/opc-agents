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
        error_class = _error_class_for_code(self.code)
        payload: dict[str, Any] = {
            "status": "error",
            "code": self.code,
            "error_class": error_class,
            "retryable": error_class in {"project_busy", "runtime_failed"},
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        if self.suggested_action:
            payload["suggested_action"] = self.suggested_action
        return payload


def _error_class_for_code(code: str) -> str:
    normalized = str(code or "").strip().casefold()
    if normalized in {"project_busy", "control_step_limit_exceeded"}:
        return "project_busy"
    if normalized.endswith("_invalid") or "invalid" in normalized or normalized in {"review_assessment_invalid", "stage_output_invalid"}:
        return "payload_invalid"
    if normalized in {"review_task_not_pending", "review_task_missing", "requirements_missing", "architecture_missing", "context_missing", "work_items_missing"}:
        return "state_conflict"
    if "precondition" in normalized or "semantic" in normalized or "limit_reached" in normalized:
        return "precondition_failed"
    if "runtime" in normalized or "failed" in normalized:
        return "runtime_failed"
    return "unknown"


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
            "error_class": "unhandled_exception",
            "retryable": False,
            "message": str(exc),
            **({"details": details} if details else {}),
        },
        1,
    )