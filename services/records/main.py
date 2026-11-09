"""Academic Records Service: grades and transcript generation."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Literal

from fastapi import Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ftcommon import config
from ftcommon.app import create_app
from ftcommon.db import get_db, served_from_var
from ftcommon.logs import log_event
from ftcommon.metrics import DEGRADED
from ftcommon.resilience import DependencyError, ServiceClient
from ftcommon.security import Principal, audit, get_principal, require_roles, require_self_or_staff
from ftcommon.validation import validate_payload

app = create_app("records-service")
db = get_db()
student_client = ServiceClient("student-service", f"{config.INTERNAL_GATEWAY}/student")

GRADE_POINTS = {"A": 4.0, "A-": 3.67, "B+": 3.33, "B": 3.0, "B-": 2.67, "C+": 2.33, "C": 2.0,
                "C-": 1.67, "D+": 1.33, "D": 1.0, "F": 0.0}

# last-known-good student profiles, used when student-service is unavailable
_profile_cache: dict[int, dict] = {}
_cache_lock = threading.Lock()


class GradeIn(BaseModel):
    student_id: int = Field(ge=1000, le=999999)
    course_code: str = Field(pattern=r"^[A-Z]{2,4}[0-9]{3}$")
    term: str = Field(pattern=r"^(Fall|Spring|Summer)-20[2-3][0-9]$")
    grade: Literal["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F"]


class LooseGradeIn(BaseModel):
    student_id: int
    course_code: str
    term: str
    grade: str


def _student_profile(student_id: int) -> tuple[dict, bool]:
    """Return (profile, from_cache)."""
    try:
        resp = student_client.get(f"/internal/students/{student_id}")
    except DependencyError as exc:
        if not config.RESILIENT:
            raise
        with _cache_lock:
            cached = _profile_cache.get(student_id)
        if cached is None:
            raise
        DEGRADED.labels(config.SERVICE_NAME, "student_profile_cache").inc()
        log_event("graceful_degradation", feature="student_profile_cache", reason=exc.kind)
        return cached, True
    if resp.status_code == 404:
        raise HTTPException(404, "student not found")
    profile = resp.json()
    with _cache_lock:
        _profile_cache[student_id] = profile
    return profile, False


@app.post("/api/records/grades")
def upsert_grade(request: Request, payload: dict = Body(...), p: Principal = Depends(get_principal)):
    require_roles(p, request, "admin", "registrar")
    g = validate_payload(GradeIn, LooseGradeIn, payload)
    with db.transaction() as conn:
        # natural key (student, course, term) makes this write idempotent
        conn.execute(
            "INSERT INTO grades (student_id, course_code, term, grade) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (student_id, course_code, term) DO UPDATE SET grade = EXCLUDED.grade, updated_at = now()",
            (g.student_id, g.course_code, g.term, g.grade))
        audit("grade.upsert", "success", resource=f"grade:{g.student_id}:{g.course_code}:{g.term}",
              principal=p, conn=conn, grade=g.grade)
    return {"status": "saved", "student_id": g.student_id, "course_code": g.course_code, "term": g.term}


@app.get("/api/records/transcripts/{student_id}")
def transcript(student_id: int, request: Request, p: Principal = Depends(get_principal)):
    require_self_or_staff(p, request, student_id)
    profile, from_cache = _student_profile(student_id)
    rows = db.query(
        "SELECT g.course_code, c.title, c.credits, g.term, g.grade FROM grades g "
        "JOIN courses c ON c.code = g.course_code WHERE g.student_id = %s ORDER BY g.term, g.course_code",
        (student_id,))
    served_from = served_from_var.get()
    credits = sum(r["credits"] for r in rows if r["grade"] in GRADE_POINTS)
    points = sum(GRADE_POINTS[r["grade"]] * r["credits"] for r in rows if r["grade"] in GRADE_POINTS)
    degraded = []
    if from_cache:
        degraded.append("student_profile_cached")
    if served_from == "replica":
        degraded.append("read_from_replica")
    return {
        "student": {k: profile.get(k) for k in ("id", "first_name", "last_name", "program")},
        "courses": rows,
        "total_credits": credits,
        "gpa": round(points / credits, 2) if credits else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "served_from": served_from,
        "degraded": degraded,
    }
