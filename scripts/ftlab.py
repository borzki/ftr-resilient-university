"""Experiment harness: environment control, fault injection, open-loop workload
generation, alert polling and data-integrity checks.

All measurements are taken from real requests against the local Docker stack.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import httpx

ROOT = Path(__file__).resolve().parent.parent
# overridable so the load generator can also run inside the Docker network (E5)
GATEWAY = os.getenv("FTLAB_GATEWAY", "http://127.0.0.1:18080")
PROM = os.getenv("FTLAB_PROM", "http://127.0.0.1:19090")
ENV = dict(line.strip().split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if "=" in line)

APP_CONTAINERS = {
    "baseline": ["student-1", "payment-1", "records-1", "timetable-1"],
    "resilient": ["student-1", "student-2", "payment-1", "payment-2", "records-1", "records-2", "timetable-1"],
}
ALL_CONTAINERS = {
    "baseline": APP_CONTAINERS["baseline"] + ["pg-primary", "gateway", "prometheus"],
    "resilient": APP_CONTAINERS["resilient"] + ["pg-primary", "pg-replica", "gateway", "prometheus"],
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ docker
def sh(args: list[str], check: bool = True, timeout: float = 120) -> str:
    res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {res.stderr.strip()[:500]}")
    return res.stdout.strip()


def compose(mode: str, *args: str, timeout: float = 600) -> str:
    return sh(["docker", "compose", "-f", str(ROOT / f"docker-compose.{mode}.yml"), *args], timeout=timeout)


def container_state(name: str) -> tuple[bool, str]:
    out = sh(["docker", "inspect", "-f", "{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
              name], check=False)
    running, _, health = out.partition("|")
    return running == "true", health


def psql(sql: str) -> str:
    return sh(["docker", "exec", "pg-primary", "psql", "-U", "university", "-d", "university", "-At", "-F", "|",
               "-c", sql])


def integrity() -> dict:
    cols = ["payment_rows", "duplicated_refs", "duplicate_rows", "balance_mismatches", "invalid_payments",
            "invalid_grades", "grades_checksum"]
    vals = psql("SELECT * FROM integrity_report").split("|")
    out = dict(zip(cols, vals))
    for k in cols[:-1]:
        out[k] = int(out[k])
    out["total_paid"] = psql("SELECT coalesce(sum(total_paid),0) FROM student_accounts")
    out["sum_payments"] = psql("SELECT coalesce(sum(amount),0) FROM payments")
    out["audit_denied"] = int(psql("SELECT count(*) FROM audit_log WHERE outcome='denied'"))
    out["audit_login_failures"] = int(psql("SELECT count(*) FROM audit_log WHERE action='auth.login' AND outcome='failure'"))
    return out


_CHAOS_PY = ("import os,sys,json,urllib.request as u;"
             "r=u.Request('http://localhost:8000/chaos',data=sys.argv[1].encode(),"
             "headers={'Content-Type':'application/json','X-Chaos-Token':os.environ['CHAOS_TOKEN']},method='POST');"
             "print(u.urlopen(r,timeout=3).read().decode())")


def chaos(container: str, **settings) -> None:
    body = {"latency_ms": 0, "error_rate": 0.0, "fail_after_commit_rate": 0.0, "crash_mid_txn_rate": 0.0, **settings}
    sh(["docker", "exec", container, "python", "-c", _CHAOS_PY, json.dumps(body)])


def crash_app(container: str) -> None:
    """Abrupt process death (SIGKILL) inside the container."""
    sh(["docker", "exec", container, "sh", "-c", "kill -9 $(cat /tmp/app.pid)"], check=False)


def crash_db() -> None:
    sh(["docker", "exec", "pg-primary", "sh", "-c",
        "kill -9 $(head -1 /var/lib/postgresql/data/postmaster.pid)"], check=False)


def start(*names: str) -> None:
    sh(["docker", "start", *names])


# ---------------------------------------------------------- environment
def bring_up(mode: str) -> None:
    other = "baseline" if mode == "resilient" else "resilient"
    log(f"stopping {other} stack (if running)")
    compose(other, "down", "-v", "--remove-orphans")
    log(f"starting {mode} stack")
    compose(mode, "down", "-v", "--remove-orphans")
    compose(mode, "up", "-d", "--build")
    wait_ready(mode, timeout=180)


def wait_ready(mode: str, timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok = True
        for c in ALL_CONTAINERS[mode]:
            running, health = container_state(c)
            if not running or health not in ("", "healthy"):
                ok = False
                break
        if ok:
            try:
                r = httpx.get(f"{GATEWAY}/health", timeout=2)
                targets = httpx.get(f"{PROM}/api/v1/query", params={"query": 'up{job=~".*-service"}'}, timeout=2).json()
                ups = [x["value"][1] for x in targets["data"]["result"]]
                if r.status_code == 200 and ups and all(u == "1" for u in ups) and len(ups) == len(APP_CONTAINERS[mode]):
                    return
            except Exception:
                pass
        time.sleep(1)
    raise RuntimeError(f"{mode} stack not ready after {timeout}s")


def firing_alerts() -> list[dict]:
    try:
        data = httpx.get(f"{PROM}/api/v1/alerts", timeout=2).json()["data"]["alerts"]
        return [a for a in data if a["state"] in ("firing", "pending")]
    except Exception:
        return []


def reset_state(mode: str) -> None:
    """Restore a known state: all containers running, chaos off, DB reset, no alerts."""
    for c in ALL_CONTAINERS[mode]:
        sh(["docker", "unpause", c], check=False)
        running, _ = container_state(c)
        if not running:
            log(f"reset: starting {c}")
            start(c)
    wait_ready(mode, timeout=120)
    for c in APP_CONTAINERS[mode]:
        chaos(c)
    psql("SELECT reset_test_state()")
    deadline = time.time() + 90
    while time.time() < deadline and firing_alerts():
        time.sleep(2)
    left = firing_alerts()
    if left:
        log(f"reset: alerts still active: {[a['labels']['alertname'] for a in left]}")


# ---------------------------------------------------------------- users
@dataclass
class User:
    username: str
    token: str
    student_id: int | None


_token_cache: dict[str, dict[str, User]] = {}


def login_all(mode: str, n_students: int = 50) -> dict[str, User]:
    if mode in _token_cache:
        return _token_cache[mode]
    users: dict[str, User] = {}
    with httpx.Client(base_url=GATEWAY, timeout=10) as c:
        names = ["admin", "registrar"] + [f"s{1000 + i}" for i in range(1, n_students + 1)]
        for name in names:
            for _ in range(30):
                r = c.post("/api/auth/login", json={"username": name, "password": ENV["SEED_PASSWORD"]})
                if r.status_code == 200:
                    d = r.json()
                    users[name] = User(name, d["access_token"], d["student_id"])
                    break
                if r.status_code == 429:
                    time.sleep(1.1)
                    continue
                raise RuntimeError(f"login {name}: {r.status_code} {r.text}")
    _token_cache[mode] = users
    return users


def students(users: dict[str, User]) -> list[User]:
    return [u for u in users.values() if u.student_id]


# ------------------------------------------------------------ workload
@dataclass
class Record:
    t: float            # send time relative to experiment start (s)
    label: str
    status: int         # HTTP status, 0 = client-side error (timeout / connection)
    latency: float      # seconds
    degraded: bool = False
    error: str = ""


def auth(u: User) -> dict:
    return {"Authorization": f"Bearer {u.token}"}


def op_profile(u: User):
    return "GET", f"/api/students/{u.student_id}", {"headers": auth(u)}, "profile"


def op_dashboard(u: User):
    return "GET", f"/api/dashboard/{u.student_id}", {"headers": auth(u)}, "dashboard"


def op_transcript(u: User):
    return "GET", f"/api/records/transcripts/{u.student_id}", {"headers": auth(u)}, "transcript"


def op_timetable(u: User):
    return "GET", f"/api/timetable/{u.student_id}", {"headers": auth(u)}, "timetable"


def op_payment(u: User):
    headers = {**auth(u), "Idempotency-Key": uuid.uuid4().hex}
    body = {"student_id": u.student_id, "amount": "10.00", "term": "Fall-2026", "method": "card"}
    return "POST", "/api/payments", {"headers": headers, "json": body}, "payment"


MIX_DEFAULT = [(30, op_profile), (25, op_dashboard), (20, op_transcript), (15, op_timetable), (10, op_payment)]
MIX_READ = [(30, op_profile), (25, op_dashboard), (25, op_transcript), (20, op_timetable)]


def _pick(mix, rnd: random.Random):
    total = sum(w for w, _ in mix)
    x = rnd.uniform(0, total)
    for w, op in mix:
        x -= w
        if x <= 0:
            return op
    return mix[-1][1]


@dataclass
class Event:
    t: float
    name: str
    detail: str = ""


@dataclass
class RunResult:
    records: list[Record] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    alerts: dict = field(default_factory=dict)        # "name{labels}" -> {"pending": t, "firing": t, "resolved": t}
    samples: dict = field(default_factory=dict)


async def _send(client: httpx.AsyncClient, method, path, kwargs, label, t0, out: list[Record],
                deadline: float | None = None):
    t_send = time.perf_counter()
    try:
        if deadline:  # cap the total request time (httpx timeouts are per phase)
            r = await asyncio.wait_for(client.request(method, path, **kwargs), timeout=deadline)
        else:
            r = await client.request(method, path, **kwargs)
        degraded = False
        if r.status_code == 200 and label in ("dashboard", "transcript"):
            try:
                degraded = bool(r.json().get("degraded"))
            except Exception:
                pass
        out.append(Record(round(t_send - t0, 4), label, r.status_code, time.perf_counter() - t_send, degraded))
    except Exception as exc:
        out.append(Record(round(t_send - t0, 4), label, 0, time.perf_counter() - t_send, False, type(exc).__name__))


async def _alert_poller(t0: float, result: RunResult, stop: asyncio.Event):
    async with httpx.AsyncClient(timeout=2) as c:
        while not stop.is_set():
            now = time.perf_counter() - t0
            try:
                alerts = (await c.get(f"{PROM}/api/v1/alerts")).json()["data"]["alerts"]
            except Exception:
                alerts = []
            seen = set()
            for a in alerts:
                lbl = a["labels"]
                base = lbl["alertname"] + "{" + ",".join(f"{k}={v}" for k, v in sorted(lbl.items())
                                                        if k in ("service", "instance", "dependency")) + "}"
                key, n = base, 1
                while key in result.alerts and "resolved" in result.alerts[key]:  # a new episode
                    n += 1
                    key = f"{base}#{n}"
                seen.add(key)
                entry = result.alerts.setdefault(key, {})
                entry.setdefault(a["state"], round(now, 2))
            for key, entry in result.alerts.items():
                if key not in seen and "firing" in entry and "resolved" not in entry:
                    entry["resolved"] = round(now, 2)
            await asyncio.sleep(1)


async def run_load(*, duration: float, rate: Callable[[float], float], users: list[User], mix,
                   actions: list[tuple[float, str, Callable[[], object]]] = (),
                   background: list[Callable[[float, RunResult, asyncio.Event], Awaitable[None]]] = (),
                   timeout: float = 5.0, seed: int = 1) -> RunResult:
    """Open-loop workload: requests are sent at the target rate regardless of responses."""
    result = RunResult()
    rnd = random.Random(seed)
    limits = httpx.Limits(max_connections=2000, max_keepalive_connections=200)
    stop = asyncio.Event()
    async with httpx.AsyncClient(base_url=GATEWAY, timeout=timeout, limits=limits) as client:
        t0 = time.perf_counter()
        tasks = [asyncio.create_task(_alert_poller(t0, result, stop))]
        tasks += [asyncio.create_task(b(t0, result, stop)) for b in background]

        async def do_action(at: float, name: str, fn):
            await asyncio.sleep(max(0, at - (time.perf_counter() - t0)))
            start_t = time.perf_counter() - t0
            detail = await asyncio.to_thread(fn)
            result.events.append(Event(round(start_t, 2), name, str(detail or "")))
            log(f"  t={start_t:5.1f}s {name}")

        tasks += [asyncio.create_task(do_action(at, n, f)) for at, n, f in actions]
        inflight: set[asyncio.Task] = set()
        sent = 0.0
        last = 0.0
        while True:
            now = time.perf_counter() - t0
            if now >= duration:
                break
            sent += rate(now) * (now - last)
            last = now
            while sent >= 1:
                sent -= 1
                u = rnd.choice(users)
                method, path, kwargs, label = _pick(mix, rnd)(u)
                task = asyncio.create_task(_send(client, method, path, kwargs, label, t0, result.records, timeout))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
            await asyncio.sleep(0.005)
        if inflight:
            await asyncio.wait(set(inflight), timeout=timeout + 2)
            for t in list(inflight):
                t.cancel()
            await asyncio.gather(*inflight, return_exceptions=True)
        await asyncio.sleep(0.5)
        stop.set()
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    result.records.sort(key=lambda r: r.t)
    result.events.sort(key=lambda e: e.t)
    return result


# ------------------------------------------------------------- analysis
def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def summarize(result: RunResult, inject_t: float | None, *, ok_statuses=(200, 201)) -> dict:
    recs = result.records
    ok = [r for r in recs if r.status in ok_statuses]
    rejected = [r for r in recs if r.status == 429]
    failed = [r for r in recs if r.status not in ok_statuses and r.status != 429]
    lat = [r.latency for r in recs]
    out = {
        "requests": len(recs),
        "success": len(ok),
        "failed": len(failed),
        "rate_limited": len(rejected),
        "degraded": sum(r.degraded for r in recs),
        "success_ratio": round(len(ok) / len(recs), 4) if recs else None,
        "error_rate": round(len(failed) / len(recs), 4) if recs else None,
        "latency_avg_ms": round(statistics.mean(lat) * 1000, 1) if lat else None,
        "latency_p50_ms": round(pct(lat, 50) * 1000, 1) if lat else None,
        "latency_p95_ms": round(pct(lat, 95) * 1000, 1) if lat else None,
        "latency_p99_ms": round(pct(lat, 99) * 1000, 1) if lat else None,
        "client_timeouts": sum(1 for r in recs if r.status == 0),
        "status_counts": {},
    }
    for r in recs:
        out["status_counts"][str(r.status)] = out["status_counts"].get(str(r.status), 0) + 1
    if inject_t is not None:
        after = [r for r in recs if r.t >= inject_t]
        f_after = [r for r in after if r.status not in ok_statuses and r.status != 429]
        out["window_requests"] = len(after)
        out["window_success_ratio"] = round(sum(r.status in ok_statuses for r in after) / len(after), 4) if after else None
        out["first_failure_s"] = round(f_after[0].t - inject_t, 2) if f_after else None
        out["impact_duration_s"] = round(f_after[-1].t + f_after[-1].latency - inject_t, 2) if f_after else 0.0
    out["send_rate_rps"] = round(len(recs) / (recs[-1].t - recs[0].t), 1) if len(recs) > 1 else None
    return out


def timeseries(result: RunResult, bucket: float = 1.0) -> list[dict]:
    if not result.records:
        return []
    n = int(max(r.t for r in result.records) // bucket) + 1
    rows = [{"t": i * bucket, "ok": 0, "failed": 0, "degraded": 0, "rate_limited": 0, "lat": []} for i in range(n)]
    for r in result.records:
        b = rows[int(r.t // bucket)]
        if r.status in (200, 201):
            b["ok"] += 1
        elif r.status == 429:
            b["rate_limited"] += 1
        else:
            b["failed"] += 1
        b["degraded"] += int(r.degraded)
        b["lat"].append(r.latency)
    for b in rows:
        lat = b.pop("lat")
        b["p95_ms"] = round(pct(lat, 95) * 1000, 1) if lat else None
    return rows


def save_run(mode: str, exp: str, rep: int, result: RunResult, summary: dict, extra: dict | None = None) -> Path:
    d = ROOT / "results" / "raw" / mode
    d.mkdir(parents=True, exist_ok=True)
    base = d / f"{exp}_r{rep}"
    with open(base.with_suffix(".csv"), "w", encoding="utf-8") as fh:
        fh.write("t_s,label,status,latency_ms,degraded,error\n")
        for r in result.records:
            fh.write(f"{r.t},{r.label},{r.status},{r.latency * 1000:.1f},{int(r.degraded)},{r.error}\n")
    payload = {"mode": mode, "experiment": exp, "rep": rep, "summary": summary,
               "events": [asdict(e) for e in result.events], "alerts": result.alerts,
               "timeseries": timeseries(result), **(extra or {})}
    base.with_suffix(".json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return base
