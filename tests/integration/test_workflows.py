"""Integration/system tests for the critical workflows of the RESILIENT stack.

Run against a running stack:
    docker compose -f docker-compose.resilient.yml up -d
    python -m pytest tests/integration -q
"""
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
ENV = dict(l.strip().split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l)
GATEWAY = "http://127.0.0.1:18080"


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=GATEWAY, timeout=10) as c:
        yield c


def login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": ENV["SEED_PASSWORD"]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def student(client):
    return login(client, "s1010")


@pytest.fixture(scope="module")
def registrar(client):
    return login(client, "registrar")


def test_login_rejects_wrong_password(client):
    r = client.post("/api/auth/login", json={"username": "s1010", "password": "wrong"})
    assert r.status_code == 401


def test_student_reads_own_profile_dashboard_transcript(client, student):
    assert client.get("/api/students/1010", headers=student).json()["id"] == 1010
    dash = client.get("/api/dashboard/1010", headers=student).json()
    assert dash["profile"]["id"] == 1010 and dash["degraded"] == []
    assert dash["account"]["student_id"] == 1010 and dash["timetable"]["classes"]
    tr = client.get("/api/records/transcripts/1010", headers=student).json()
    assert tr["courses"] and tr["gpa"] is not None


def test_object_level_authorization(client, student):
    assert client.get("/api/students/1011", headers=student).status_code == 403
    assert client.get("/api/records/transcripts/1011", headers=student).status_code == 403


def test_payment_is_idempotent(client, student):
    key = uuid.uuid4().hex
    body = {"student_id": 1010, "amount": "12.50", "term": "Fall-2026", "method": "card"}
    before = client.get("/api/payments", params={"student_id": 1010}, headers=student).json()["items"]
    r1 = client.post("/api/payments", json=body, headers={**student, "Idempotency-Key": key})
    r2 = client.post("/api/payments", json=body, headers={**student, "Idempotency-Key": key})
    assert r1.status_code == 201
    assert r2.status_code == 200 and r2.headers.get("Idempotent-Replay") == "true"
    assert r1.json()["payment_id"] == r2.json()["payment_id"]
    after = client.get("/api/payments", params={"student_id": 1010}, headers=student).json()["items"]
    assert len(after) == len(before) + 1


def test_payment_requires_idempotency_key_and_valid_input(client, student):
    body = {"student_id": 1010, "amount": "12.50", "term": "Fall-2026", "method": "card"}
    assert client.post("/api/payments", json=body, headers=student).status_code == 400
    bad = {**body, "amount": "-1"}
    assert client.post("/api/payments", json=bad, headers={**student, "Idempotency-Key": uuid.uuid4().hex}).status_code == 422


def test_grade_upsert_is_role_protected_and_idempotent(client, student, registrar):
    g = {"student_id": 1010, "course_code": "CSE307", "term": "Fall-2026", "grade": "B+"}
    assert client.post("/api/records/grades", json=g, headers=student).status_code == 403
    assert client.post("/api/records/grades", json=g, headers=registrar).status_code == 200
    assert client.post("/api/records/grades", json=g, headers=registrar).status_code == 200
    courses = client.get("/api/records/transcripts/1010", headers=registrar).json()["courses"]
    assert sum(1 for c in courses if c["course_code"] == "CSE307" and c["term"] == "Fall-2026") == 1


def test_internal_and_unknown_routes_are_not_exposed(client):
    assert client.get("/internal/students/1010").status_code == 404
    assert client.get("/metrics").status_code == 404
    assert client.get("/chaos").status_code == 404


def test_request_id_is_propagated(client, student):
    r = client.get("/api/students/1010", headers={**student, "X-Request-ID": "itest-123"})
    assert "itest-123" in r.headers.get("X-Request-ID", "")


def test_unauthenticated_access_is_denied(client):
    assert client.get("/api/students/1010").status_code == 401
    assert client.get("/api/admin/audit").status_code == 401
