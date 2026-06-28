"""
Example: how to mock an external API (e.g. a payment service)

Copy this file, rename it to routers/{service_name}_api.py, and adapt it
to match the real API contract. Field types and structure must exactly match
the real API — only the data values are fake.
"""

import json
import pathlib
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["example"])

DATA_DIR = pathlib.Path(__file__).parent.parent / "data"


def load_fixture(name: str) -> dict:
    path = DATA_DIR / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


# ── Request / Response schemas (must exactly match the real API) ─────────────

class PaymentRequest(BaseModel):
    amount: float
    currency: str = "USD"
    description: Optional[str] = None
    customer_id: str


class PaymentResponse(BaseModel):
    payment_id: str
    status: str          # "pending" | "success" | "failed"
    amount: float
    currency: str
    created_at: str


# ── Mock routes (matching real API URL paths and HTTP methods) ───────────────

@router.post("/payments", response_model=PaymentResponse, status_code=201)
async def create_payment(req: PaymentRequest) -> PaymentResponse:
    # Simulate specific error scenario: amount > 99999 triggers failure
    if req.amount > 99999:
        raise HTTPException(422, detail={"code": "AMOUNT_TOO_LARGE", "message": "Amount exceeds limit"})

    return PaymentResponse(
        payment_id=f"pay_mock_{req.customer_id[:8]}",
        status="success",
        amount=req.amount,
        currency=req.currency,
        created_at="2026-01-01T00:00:00Z",
    )


@router.get("/payments/{payment_id}", response_model=PaymentResponse)
async def get_payment(payment_id: str) -> PaymentResponse:
    if payment_id == "pay_not_found":
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Payment not found"})

    return PaymentResponse(
        payment_id=payment_id,
        status="success",
        amount=100.0,
        currency="USD",
        created_at="2026-01-01T00:00:00Z",
    )
