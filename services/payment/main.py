"""Payment Service: tuition payments with idempotency keys and atomic transactions."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from fastapi import Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ftcommon import chaos, config
from ftcommon.app import create_app
from ftcommon.db import get_db, served_from_var
from ftcommon.logs import log_event, request_id_var
from ftcommon.metrics import DEGRADED, PAYMENTS
from ftcommon.resilience import DependencyError, ServiceClient
from ftcommon.security import Principal, audit, get_principal, require_internal, require_self_or_staff
from ftcommon.validation import validate_payload

app = create_app("payment-service")
db = get_db()
student_client = ServiceClient("student-service", f"{config.INTERNAL_GATEWAY}/student")

IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"


class PaymentIn(BaseModel):
    student_id: int = Field(ge=1000, le=999999)
    amount: Decimal = Field(gt=0, le=Decimal("100000"), max_digits=12, decimal_places=2)
    term: str = Field(pattern=r"^(Fall|Spring|Summer)-20[2-3][0-9]$")
    method: Literal["card", "bank_transfer", "cash"]


class LoosePaymentIn(BaseModel):
    student_id: int
    amount: Decimal
    term: str
    method: str


def _payment_json(row: dict) -> dict:
    return {"payment_id": row["id"], "student_id": row["student_id"], "amount": str(row["amount"]),
            "term": row["term"], "method": row["method"], "created_at": row["created_at"].isoformat()}


def _verify_student(student_id: int) -> str:
    """Check the student is active via student-service; degrade to local account check."""
    try:
        resp = student_client.get(f"/internal/students/{student_id}")
    except DependencyError as exc:
        if not config.RESILIENT:
            raise
        DEGRADED.labels(config.SERVICE_NAME, "student_verification").inc()
        log_event("graceful_degradation", feature="student_verification", reason=exc.kind)
        row = db.query("SELECT 1 FROM student_accounts WHERE student_id = %s", (student_id,), one=True)
        if not row:
            raise HTTPException(404, "student not found")
        return "local_account_check"
    if resp.status_code == 404:
        raise HTTPException(404, "student not found")
    if resp.json().get("status") != "active":
        raise HTTPException(409, "student is not active")
    return "student_service"


@app.post("/api/payments", status_code=201)
def create_payment(request: Request, payload: dict = Body(...), p: Principal = Depends(get_principal),
                   idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    pay = validate_payload(PaymentIn, LoosePaymentIn, payload)
    require_self_or_staff(p, request, pay.student_id)
    if config.RESILIENT:
        if not idempotency_key or not re.match(IDEMPOTENCY_PATTERN, idempotency_key):
            raise HTTPException(400, "a valid Idempotency-Key header is required")
    verified_by = _verify_student(pay.student_id)
    rid = request_id_var.get()

    if config.RESILIENT:
        # One atomic transaction: payment row + balance update + audit record.
        with db.transaction() as conn:
            existing = conn.execute("SELECT * FROM payments WHERE idempotency_key = %s",
                                    (idempotency_key,)).fetchone()
            if existing:
                PAYMENTS.labels(config.SERVICE_NAME, "duplicate_prevented").inc()
                log_event("duplicate_payment_prevented", idempotency_key=idempotency_key)
                return JSONResponse(_payment_json(existing), status_code=200,
                                    headers={"Idempotent-Replay": "true"})
            row = conn.execute(
                "INSERT INTO payments (student_id, amount, term, method, client_ref, idempotency_key, request_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                (pay.student_id, pay.amount, pay.term, pay.method, idempotency_key, idempotency_key, rid),
            ).fetchone()
            if row is None:  # concurrent duplicate won the race
                PAYMENTS.labels(config.SERVICE_NAME, "duplicate_prevented").inc()
                existing = conn.execute("SELECT * FROM payments WHERE idempotency_key = %s",
                                        (idempotency_key,)).fetchone()
                return JSONResponse(_payment_json(existing), status_code=200,
                                    headers={"Idempotent-Replay": "true"})
            if chaos.roll("crash_mid_txn_rate"):
                chaos.crash_now("between payment insert and balance update")
            updated = conn.execute("UPDATE student_accounts SET total_paid = total_paid + %s "
                                   "WHERE student_id = %s", (pay.amount, pay.student_id)).rowcount
            if updated != 1:
                raise HTTPException(404, "student account not found")  # rolls back the insert
            audit("payment.create", "success", resource=f"payment:{row['id']}", principal=p, conn=conn,
                  amount=str(pay.amount), verified_by=verified_by)
    else:
        # Baseline: no idempotency, each statement commits separately (no atomicity).
        with db.autocommit() as conn:
            row = conn.execute(
                "INSERT INTO payments (student_id, amount, term, method, client_ref, request_id) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (pay.student_id, pay.amount, pay.term, pay.method, idempotency_key, rid),
            ).fetchone()
            if chaos.roll("crash_mid_txn_rate"):
                chaos.crash_now("between payment insert and balance update")
            conn.execute("UPDATE student_accounts SET total_paid = total_paid + %s WHERE student_id = %s",
                         (pay.amount, pay.student_id))
        audit("payment.create", "success", resource=f"payment:{row['id']}", principal=p,
              amount=str(pay.amount))

    if chaos.roll("fail_after_commit_rate"):
        # the payment is committed but the client never gets the confirmation
        log_event("chaos_response_lost", payment_id=row["id"])
        raise HTTPException(503, "injected fault: response lost after commit")
    PAYMENTS.labels(config.SERVICE_NAME, "created").inc()
    return _payment_json(row)


@app.get("/api/payments")
def list_payments(request: Request, student_id: int = Query(..., ge=1), p: Principal = Depends(get_principal)):
    require_self_or_staff(p, request, student_id)
    rows = db.query("SELECT * FROM payments WHERE student_id = %s ORDER BY id DESC LIMIT 100", (student_id,))
    return {"items": [_payment_json(r) for r in rows], "served_from": served_from_var.get()}


@app.get("/internal/accounts/{student_id}")
def account(student_id: int, request: Request):
    require_internal(request)
    row = db.query("SELECT student_id, tuition_due, total_paid FROM student_accounts WHERE student_id = %s",
                   (student_id,), one=True)
    if not row:
        raise HTTPException(404, "account not found")
    return {"student_id": row["student_id"], "tuition_due": str(row["tuition_due"]),
            "total_paid": str(row["total_paid"]),
            "balance": str(row["tuition_due"] - row["total_paid"]), "served_from": served_from_var.get()}
