import time

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ftcommon import config
from ftcommon.security import Principal, create_token, get_principal, require_internal, require_self_or_staff


def make_request(headers: dict | None = None, path: str = "/api/test") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": path, "headers": raw, "query_string": b""})


def test_valid_token_is_accepted():
    token = create_token("s1001", "student", 1001)
    p = get_principal(make_request({"Authorization": f"Bearer {token}"}))
    assert p == Principal("s1001", "student", 1001)


@pytest.mark.parametrize("header", [None, "Basic abc", "Bearer not-a-jwt"])
def test_missing_or_malformed_token_is_rejected(header):
    headers = {"Authorization": header} if header else {}
    with pytest.raises(HTTPException) as exc:
        get_principal(make_request(headers))
    assert exc.value.status_code == 401


def test_expired_token_is_rejected():
    now = int(time.time())
    token = jwt.encode({"sub": "s1", "role": "student", "iss": config.JWT_ISSUER, "iat": now - 100,
                        "exp": now - 10}, config.JWT_SECRET, algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        get_principal(make_request({"Authorization": f"Bearer {token}"}))
    assert exc.value.status_code == 401


def test_token_signed_with_other_secret_is_rejected():
    now = int(time.time())
    token = jwt.encode({"sub": "x", "role": "admin", "iss": config.JWT_ISSUER, "iat": now, "exp": now + 60},
                       "attacker-secret-attacker-secret-attacker", algorithm="HS256")
    with pytest.raises(HTTPException):
        get_principal(make_request({"Authorization": f"Bearer {token}"}))


def test_alg_none_token_is_rejected():
    now = int(time.time())
    token = jwt.encode({"sub": "x", "role": "admin", "iss": config.JWT_ISSUER, "iat": now, "exp": now + 60},
                       None, algorithm="none")
    with pytest.raises(HTTPException):
        get_principal(make_request({"Authorization": f"Bearer {token}"}))


def test_student_cannot_access_other_student():
    with pytest.raises(HTTPException) as exc:
        require_self_or_staff(Principal("s1001", "student", 1001), make_request(), 1002)
    assert exc.value.status_code == 403
    require_self_or_staff(Principal("s1001", "student", 1001), make_request(), 1001)
    require_self_or_staff(Principal("reg", "registrar"), make_request(), 1002)


def test_internal_endpoint_requires_service_token():
    with pytest.raises(HTTPException):
        require_internal(make_request({"X-Service-Token": "wrong"}))
    require_internal(make_request({"X-Service-Token": config.SERVICE_TOKEN}))
