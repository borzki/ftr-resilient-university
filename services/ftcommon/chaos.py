"""Controlled fault injection for experiments (enabled only with CHAOS_ENABLED=true).

The /chaos endpoint is not routed by the public gateway; the experiment
harness reaches it with `docker exec` on localhost and a separate chaos token.
"""
from __future__ import annotations

import hmac
import os
import random
import threading

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from . import config
from .logs import log_event

_lock = threading.Lock()
_state = {"latency_ms": 0, "error_rate": 0.0, "fail_after_commit_rate": 0.0, "crash_mid_txn_rate": 0.0}


class ChaosSettings(BaseModel):
    latency_ms: int = Field(0, ge=0, le=30000)
    error_rate: float = Field(0.0, ge=0, le=1)
    fail_after_commit_rate: float = Field(0.0, ge=0, le=1)
    crash_mid_txn_rate: float = Field(0.0, ge=0, le=1)


def get(name: str):
    with _lock:
        return _state[name]


def roll(name: str) -> bool:
    rate = get(name)
    return rate > 0 and random.random() < rate


def crash_now(reason: str) -> None:
    log_event("chaos_crash", reason=reason)
    os._exit(137)  # abrupt process death, like an OOM-kill or hardware fault


router = APIRouter()


def _check(token: str | None) -> None:
    if not config.CHAOS_ENABLED:
        raise HTTPException(404, "not found")
    if not config.CHAOS_TOKEN or not token or not hmac.compare_digest(token, config.CHAOS_TOKEN):
        raise HTTPException(403, "bad chaos token")


@router.get("/chaos")
def chaos_get(x_chaos_token: str | None = Header(None)):
    _check(x_chaos_token)
    with _lock:
        return dict(_state)


@router.post("/chaos")
def chaos_set(settings: ChaosSettings, x_chaos_token: str | None = Header(None)):
    _check(x_chaos_token)
    with _lock:
        _state.update(settings.model_dump())
        snapshot = dict(_state)
    log_event("chaos_configured", **snapshot)
    return snapshot
