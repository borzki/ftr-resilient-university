"""Authentication (JWT), role-based authorization, service-to-service
authentication, and audit logging."""
from __future__ import annotations

import hmac
import json
import time
import uuid
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request

from . import config
from .logs import log_event, request_id_var, user_var
from .metrics import AUDIT_EVENTS, AUTH_FAILURES

STAFF_ROLES = {"admin", "registrar"}


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    student_id: int | None = None

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES


ANONYMOUS_ADMIN = Principal("anonymous", "admin", None)  # baseline: no authentication


def create_token(username: str, role: str, student_id: int | None) -> str:
    now = int(time.time())
    claims = {
        "sub": username,
        "role": role,
        "sid": student_id,
        "iss": config.JWT_ISSUER,
        "iat": now,
        "exp": now + config.JWT_TTL_SECONDS,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, config.JWT_SECRET or "baseline-insecure-secret", algorithm="HS256")


def _deny(status: int, reason: str, request: Request, detail: str) -> HTTPException:
    AUTH_FAILURES.labels(config.SERVICE_NAME, reason).inc()
    audit("auth.denied", "denied", resource=request.url.path, reason=reason)
    return HTTPException(status_code=status, detail=detail)


def get_principal(request: Request) -> Principal:
    """FastAPI dependency: authenticate the caller from the Bearer token."""
    if not config.AUTH_ENABLED:
        return ANONYMOUS_ADMIN
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise _deny(401, "missing_token", request, "authentication required")
    token = header[7:].strip()
    try:
        claims = jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=["HS256"],  # explicit allow-list blocks alg=none / alg confusion
            issuer=config.JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "role"]},
        )
    except jwt.ExpiredSignatureError:
        raise _deny(401, "expired_token", request, "token expired")
    except jwt.InvalidTokenError:
        raise _deny(401, "invalid_token", request, "invalid token")
    principal = Principal(claims["sub"], claims["role"], claims.get("sid"))
    user_var.set(principal.username)
    request.state.user = principal.username
    return principal


def require_roles(principal: Principal, request: Request, *roles: str) -> None:
    if config.AUTH_ENABLED and principal.role not in roles:
        raise _deny(403, "forbidden_role", request, "insufficient role")


def require_self_or_staff(principal: Principal, request: Request, student_id: int) -> None:
    """Object-level authorization: students may only access their own records."""
    if not config.AUTH_ENABLED or principal.is_staff:
        return
    if principal.student_id != student_id:
        raise _deny(403, "forbidden_object", request, "access to another student's data is not allowed")


def require_internal(request: Request) -> None:
    """Service-to-service authentication for /internal endpoints."""
    if not config.AUTH_ENABLED:
        return
    supplied = request.headers.get("x-service-token", "")
    if not config.SERVICE_TOKEN or not hmac.compare_digest(supplied, config.SERVICE_TOKEN):
        raise _deny(403, "bad_service_token", request, "internal endpoint")


# ------------------------------------------------------------------ audit
def audit(action: str, outcome: str, resource: str = "", principal: Principal | None = None,
          conn=None, **details) -> None:
    """Write an audit event to the structured log and (best effort) to audit_log."""
    actor = principal.username if principal else user_var.get()
    role = principal.role if principal else "-"
    AUDIT_EVENTS.labels(config.SERVICE_NAME, action, outcome).inc()
    log_event("audit", audit_action=action, outcome=outcome, resource=resource, actor=actor, role=role, **details)
    sql = ("INSERT INTO audit_log (service, actor, role, action, resource, outcome, request_id, details) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)")
    params = (config.SERVICE_NAME, actor, role, action, resource, outcome, request_id_var.get(),
              json.dumps(details, default=str))
    if conn is not None:
        # same transaction as the business operation: fail closed (no unaudited writes)
        conn.execute(sql, params)
        return
    try:
        from .db import get_db
        with get_db().primary.connection(timeout=1.0) as c:
            c.execute(sql, params)
    except Exception as exc:  # audit storage must never break request handling
        log_event("audit_store_failed", error=str(exc))
