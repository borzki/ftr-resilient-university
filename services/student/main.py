"""Student Service: authentication, student profiles, dashboard aggregation, audit log."""
from __future__ import annotations

from fastapi import Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field

from ftcommon import config
from ftcommon.app import create_app
from ftcommon.db import get_db, served_from_var
from ftcommon.logs import log_event
from ftcommon.metrics import AUTH_FAILURES, DEGRADED
from ftcommon.resilience import DependencyError, ServiceClient
from ftcommon.validation import validate_payload
from ftcommon.security import (Principal, audit, create_token, get_principal, require_internal,
                               require_roles, require_self_or_staff)

app = create_app("student-service")
db = get_db()
payment_client = ServiceClient("payment-service", f"{config.INTERNAL_GATEWAY}/payment")
# timetable is non-critical: one attempt with a short timeout, then degrade
timetable_client = ServiceClient("timetable-service", f"{config.INTERNAL_GATEWAY}/timetable",
                                 attempts=1, timeout_s=0.8, failure_threshold=3)

STUDENT_COLUMNS = "id, first_name, last_name, email, program, year, status"


# ------------------------------------------------------------------ models
class LoginIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=128)


class LooseLoginIn(BaseModel):
    username: str
    password: str


class StudentIn(BaseModel):
    id: int = Field(ge=1000, le=999999)
    first_name: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z' -]+$")
    last_name: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z' -]+$")
    email: EmailStr
    program: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z &-]+$")
    year: int = Field(ge=1, le=6)


class LooseStudentIn(BaseModel):
    id: int
    first_name: str
    last_name: str
    email: str
    program: str
    year: int


# --------------------------------------------------------------- endpoints
@app.post("/api/auth/login")
def login(request: Request, payload: dict = Body(...)):
    data = validate_payload(LoginIn, LooseLoginIn, payload)
    row = db.query(
        "SELECT username, role, student_id FROM users "
        "WHERE username = %s AND password_hash = crypt(%s, password_hash)",
        (data.username, data.password), one=True)
    if not row:
        AUTH_FAILURES.labels(config.SERVICE_NAME, "bad_credentials").inc()
        audit("auth.login", "failure", resource="/api/auth/login", attempted_user=data.username[:64])
        raise HTTPException(401, "invalid username or password")
    token = create_token(row["username"], row["role"], row["student_id"])
    audit("auth.login", "success", resource="/api/auth/login",
          principal=Principal(row["username"], row["role"], row["student_id"]))
    return {"access_token": token, "token_type": "bearer", "role": row["role"],
            "student_id": row["student_id"], "expires_in": config.JWT_TTL_SECONDS}


@app.get("/api/students")
def list_students(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                  p: Principal = Depends(get_principal)):
    require_roles(p, request, "admin", "registrar")
    rows = db.query(f"SELECT {STUDENT_COLUMNS} FROM students ORDER BY id LIMIT %s OFFSET %s", (limit, offset))
    return {"items": rows, "served_from": served_from_var.get()}


@app.get("/api/students/{student_id}")
def get_student(student_id: int, request: Request, p: Principal = Depends(get_principal)):
    require_self_or_staff(p, request, student_id)
    row = db.query(f"SELECT {STUDENT_COLUMNS} FROM students WHERE id = %s", (student_id,), one=True)
    if not row:
        raise HTTPException(404, "student not found")
    return {**row, "served_from": served_from_var.get()}


@app.post("/api/students", status_code=201)
def create_student(request: Request, payload: dict = Body(...), p: Principal = Depends(get_principal)):
    require_roles(p, request, "admin", "registrar")
    s = validate_payload(StudentIn, LooseStudentIn, payload)
    with db.transaction() as conn:
        exists = conn.execute("SELECT 1 FROM students WHERE id = %s", (s.id,)).fetchone()
        if exists:
            raise HTTPException(409, "student already exists")
        conn.execute("INSERT INTO students (id, first_name, last_name, email, program, year) "
                     "VALUES (%s,%s,%s,%s,%s,%s)",
                     (s.id, s.first_name, s.last_name, str(s.email), s.program, s.year))
    audit("student.create", "success", resource=f"student:{s.id}", principal=p)
    return {"id": s.id, "status": "created"}


@app.get("/api/dashboard/{student_id}")
def dashboard(student_id: int, request: Request, p: Principal = Depends(get_principal)):
    """Aggregates profile (critical), account balance and timetable (non-critical)."""
    require_self_or_staff(p, request, student_id)
    profile = db.query(f"SELECT {STUDENT_COLUMNS} FROM students WHERE id = %s", (student_id,), one=True)
    if not profile:
        raise HTTPException(404, "student not found")
    degraded: list[str] = []
    result = {"profile": profile, "account": None, "timetable": None}

    if config.RESILIENT:
        try:
            result["account"] = payment_client.get(f"/internal/accounts/{student_id}").json()
        except DependencyError as exc:
            degraded.append("account")
            DEGRADED.labels(config.SERVICE_NAME, "account").inc()
            log_event("graceful_degradation", feature="account", reason=exc.kind)
        try:
            result["timetable"] = timetable_client.get(f"/internal/timetable/{student_id}").json()
        except DependencyError as exc:
            degraded.append("timetable")
            DEGRADED.labels(config.SERVICE_NAME, "timetable").inc()
            log_event("graceful_degradation", feature="timetable", reason=exc.kind)
    else:
        # baseline: any dependency failure fails the whole request
        result["account"] = payment_client.get(f"/internal/accounts/{student_id}").json()
        result["timetable"] = timetable_client.get(f"/internal/timetable/{student_id}").json()

    result["degraded"] = degraded
    return result


@app.get("/api/admin/audit")
def audit_log(request: Request, limit: int = Query(50, ge=1, le=500), p: Principal = Depends(get_principal)):
    require_roles(p, request, "admin")
    rows = db.query("SELECT id, ts, service, actor, role, action, resource, outcome, request_id "
                    "FROM audit_log ORDER BY id DESC LIMIT %s", (limit,))
    return {"items": rows}


@app.get("/internal/students/{student_id}")
def internal_student(student_id: int, request: Request):
    require_internal(request)
    row = db.query(f"SELECT {STUDENT_COLUMNS} FROM students WHERE id = %s", (student_id,), one=True)
    if not row:
        raise HTTPException(404, "student not found")
    return row
