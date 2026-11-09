"""Timetable Service (non-critical): weekly class schedule per student."""
from __future__ import annotations

from fastapi import Depends, Request

from ftcommon.app import create_app
from ftcommon.db import get_db, served_from_var
from ftcommon.security import Principal, get_principal, require_internal, require_self_or_staff

app = create_app("timetable-service")
db = get_db()

SQL = ("SELECT c.code AS course_code, c.title, c.day, to_char(c.start_time, 'HH24:MI') AS start_time, c.room "
       "FROM enrollments e JOIN courses c ON c.code = e.course_code "
       "WHERE e.student_id = %s AND e.term = 'Fall-2026' ORDER BY c.day, c.start_time")


def _timetable(student_id: int) -> dict:
    rows = db.query(SQL, (student_id,))
    return {"student_id": student_id, "term": "Fall-2026", "classes": rows, "served_from": served_from_var.get()}


@app.get("/api/timetable/{student_id}")
def timetable(student_id: int, request: Request, p: Principal = Depends(get_principal)):
    require_self_or_staff(p, request, student_id)
    return _timetable(student_id)


@app.get("/internal/timetable/{student_id}")
def internal_timetable(student_id: int, request: Request):
    require_internal(request)
    return _timetable(student_id)
