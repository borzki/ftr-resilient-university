"""Run the fault-injection and security experiments against one configuration.

Usage (from the project root, with the virtualenv active):
    python scripts/run_experiments.py --mode baseline
    python scripts/run_experiments.py --mode resilient
    python scripts/run_experiments.py --mode resilient --only E1,E3 --reps 1

Results are written to results/raw/<mode>/ (per-request CSV + JSON summary).
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures as cf
import json
import random
import threading
import time
import uuid

import httpx
import jwt

import ftlab as L

INJECT = 10.0  # seconds after the start of the workload


# ------------------------------------------------------------ helpers
def health_probe(container: str) -> bool:
    if container.startswith("pg-"):
        cmd = ["docker", "exec", container, "pg_isready", "-q", "-U", "university", "-d", "university"]
    else:
        cmd = ["docker", "exec", container, "python", "-c",
               "import urllib.request as u;u.urlopen('http://localhost:8000/health/live',timeout=1)"]
    try:
        return L.subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def watch(containers: list[str]):
    """Background task: record up/down transitions of containers (health probe ~every 0.5 s)."""
    async def bg(t0, result, stop):
        state = {c: True for c in containers}
        trans = result.samples.setdefault("transitions", [])
        while not stop.is_set():
            for c in containers:
                ok = await asyncio.to_thread(health_probe, c)
                if ok != state[c]:
                    state[c] = ok
                    trans.append({"t": round(time.perf_counter() - t0, 2), "container": c, "up": ok})
            await asyncio.sleep(0.3)
    return bg


def operator(delay: float = 10.0):
    """Baseline has no self-healing: a simulated operator restarts the failed
    component `delay` seconds after the corresponding alert fires."""
    async def bg(t0, result, stop):
        handled = set()

        async def restart(target: str):
            await asyncio.sleep(delay)
            await asyncio.to_thread(L.sh, ["docker", "start", target], False)
            t = round(time.perf_counter() - t0, 2)
            result.events.append(L.Event(t, "operator_restart", target))
            L.log(f"  t={t:5.1f}s operator restarted {target}")

        while not stop.is_set():
            for key, v in list(result.alerts.items()):
                if "firing" not in v or key in handled:
                    continue
                target = None
                if key.startswith("ServiceInstanceDown{instance="):
                    target = key.split("instance=")[1].split(":")[0]
                elif key.startswith("DatabasePrimaryDown"):
                    target = "pg-primary"
                if target and not L.container_state(target)[0]:
                    handled.add(key)
                    result.events.append(L.Event(v["firing"], "operator_alerted", target))
                    asyncio.create_task(restart(target))
            await asyncio.sleep(0.5)
    return bg


def alert_delay(result, prefix: str, inject: float) -> float | None:
    times = [v["firing"] for k, v in result.alerts.items() if k.startswith(prefix) and "firing" in v
             and v["firing"] >= inject - 1]
    return round(min(times) - inject, 2) if times else None


def recovery_time(result, container: str, inject: float) -> float | None:
    trans = [x for x in result.samples.get("transitions", []) if x["container"] == container and x["t"] >= inject - 1]
    downs = [x for x in trans if not x["up"]]
    if not downs:
        return 0.0
    ups = [x for x in trans if x["up"] and x["t"] > downs[0]["t"]]
    return round(ups[0]["t"] - inject, 2) if ups else None


def run(coro):
    return asyncio.run(coro)


def finish(mode, exp, rep, result, inject, extra=None):
    summary = L.summarize(result, inject)
    summary["integrity"] = L.integrity()
    if extra:
        summary.update(extra)
    L.save_run(mode, exp, rep, result, summary)
    s = summary
    L.log(f"  {exp} r{rep}: n={s['requests']} ok={s['success_ratio']} failed={s['failed']} "
          f"degraded={s['degraded']} p95={s['latency_p95_ms']}ms impact={s.get('impact_duration_s')}s "
          f"detect={s.get('detection_s')} recover={s.get('container_recovery_s')}")
    return summary


# --------------------------------------------------------- experiments
def e1_instance_crash(mode, rep, users):
    """Application crash: SIGKILL the student-service process on instance 1."""
    bg = [watch(["student-1"])] + ([operator()] if mode == "baseline" else [])
    res = run(L.run_load(duration=45, rate=lambda t: 30, users=L.students(users), mix=L.MIX_DEFAULT,
                         actions=[(INJECT, "crash student-1 (SIGKILL)", lambda: L.crash_app("student-1"))],
                         background=bg, seed=rep))
    return finish(mode, "E1_instance_crash", rep, res, INJECT, {
        "detection_s": alert_delay(res, "ServiceInstanceDown{instance=student-1", INJECT),
        "container_recovery_s": recovery_time(res, "student-1", INJECT)})


NODE_A = ["student-1", "payment-1", "records-1"]


def e2_node_failure(mode, rep, users):
    """Hardware/node failure: all containers of 'node A' disappear for 20 s."""
    res = run(L.run_load(duration=50, rate=lambda t: 30, users=L.students(users), mix=L.MIX_DEFAULT,
                         actions=[(INJECT, "node A down (docker stop -t 0)",
                                   lambda: L.sh(["docker", "stop", "-t", "0", *NODE_A])),
                                  (INJECT + 20, "node A repaired (docker start)", lambda: L.start(*NODE_A))],
                         background=[watch(NODE_A)], seed=rep))
    return finish(mode, "E2_node_failure", rep, res, INJECT, {
        "detection_s": alert_delay(res, "ServiceInstanceDown", INJECT),
        "container_recovery_s": recovery_time(res, "student-1", INJECT)})


def e3_dependency_timeout(mode, rep, users):
    """Dependency hang: freeze the timetable service (docker pause) for 20 s."""
    mix = [(40, L.op_dashboard), (20, L.op_profile), (20, L.op_transcript), (20, L.op_timetable)]
    res = run(L.run_load(duration=45, rate=lambda t: 30, users=L.students(users), mix=mix,
                         actions=[(INJECT, "pause timetable-1", lambda: L.sh(["docker", "pause", "timetable-1"])),
                                  (INJECT + 20, "unpause timetable-1",
                                   lambda: L.sh(["docker", "unpause", "timetable-1"]))],
                         seed=rep))
    collateral = [r for r in res.records if r.label in ("profile", "transcript") and INJECT <= r.t <= INJECT + 20]
    return finish(mode, "E3_dependency_timeout", rep, res, INJECT, {
        "detection_s": alert_delay(res, "ServiceInstanceDown{instance=timetable-1", INJECT),
        "cb_open_s": alert_delay(res, "CircuitBreakerOpen", INJECT),
        "dashboard_p95_ms_during_fault": L.summarize(L.RunResult(records=[r for r in res.records if r.label == "dashboard" and INJECT <= r.t <= INJECT + 20]), None)["latency_p95_ms"],
        "collateral_failures": sum(1 for r in collateral if r.status not in (200, 201)),
        "collateral_requests": len(collateral)})


def e4_database_failure(mode, rep, users):
    """Database crash: SIGKILL the PostgreSQL primary postmaster."""
    bg = [watch(["pg-primary"])] + ([operator()] if mode == "baseline" else [])
    res = run(L.run_load(duration=55, rate=lambda t: 30, users=L.students(users), mix=L.MIX_DEFAULT,
                         actions=[(INJECT, "crash pg-primary (SIGKILL)", L.crash_db)], background=bg, seed=rep))
    reads = [r for r in res.records if r.label != "payment" and r.t >= INJECT]
    writes = [r for r in res.records if r.label == "payment" and r.t >= INJECT]
    return finish(mode, "E4_database_failure", rep, res, INJECT, {
        "detection_s": alert_delay(res, "DatabasePrimaryDown", INJECT),
        "container_recovery_s": recovery_time(res, "pg-primary", INJECT),
        "read_success_ratio": round(sum(r.status == 200 for r in reads) / max(1, len(reads)), 4),
        "write_success_ratio": round(sum(r.status in (200, 201) for r in writes) / max(1, len(writes)), 4)})


GENERATORS = 2          # parallel in-network load generators for E5
SPIKE_PER_GEN = 150     # requests/s per generator during the spike (capacity verified by generator_control.py)
BASE_PER_GEN = 10


def e5_load_spike(mode, rep, users):
    """Load spike: 20 rps -> 300 rps for 20 s -> 20 rps (generated inside the Docker network)."""
    # The Windows host -> Docker port-forwarding path saturates at ~150-200 rps (measured), so the
    # workload is produced by GENERATORS containers on the same network as the gateway.
    cmd = ["docker", "run", "--rm", "--network", f"ftr-{mode}_uni", "-v", f"{L.ROOT}:/proj:ro", "-w", "/proj/scripts",
           "-e", "FTLAB_GATEWAY=http://gateway", "-e", "FTLAB_PROM=http://prometheus:9090",
           "ftr-university-service:latest", "python", "spike_in_network.py", mode]
    procs = [L.subprocess.Popen(cmd + [str(rep * 10 + i), str(BASE_PER_GEN), str(SPIKE_PER_GEN)],
                                stdout=L.subprocess.PIPE, stderr=L.subprocess.PIPE, text=True) for i in range(GENERATORS)]
    stats_out = {}
    t_start = time.time()

    def stats():
        time.sleep(max(0, 22 - (time.time() - t_start)))  # middle of the spike (incl. container start-up)
        stats_out["sample"] = L.sh(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}} {{.MemUsage}}"])

    th = threading.Thread(target=stats)
    th.start()
    res = L.RunResult()
    for p in procs:
        stdout, stderr = p.communicate(timeout=180)
        if p.returncode != 0:
            raise RuntimeError(f"generator failed: {stderr[-500:]}")
        data = json.loads(stdout.strip().splitlines()[-1])
        res.records += [L.Record(**r) for r in data["records"]]
        res.alerts = res.alerts or data["alerts"]
    th.join()
    res.records.sort(key=lambda r: r.t)
    res.events.append(L.Event(INJECT, "spike start (300 rps)"))
    res.events.append(L.Event(INJECT + 20, "spike end"))
    res.samples["docker_stats"] = stats_out.get("sample", "")
    spike = [r for r in res.records if INJECT <= r.t < INJECT + 20]
    s_spike = L.summarize(L.RunResult(records=spike), None)
    return finish(mode, "E5_load_spike", rep, res, INJECT, {
        "spike": {k: s_spike[k] for k in ("requests", "success_ratio", "failed", "rate_limited",
                                         "latency_avg_ms", "latency_p95_ms", "latency_p99_ms", "send_rate_rps")},
        "detection_s": alert_delay(res, "HighLatencyP95", INJECT),
        "generators": GENERATORS, "docker_stats_during_spike": res.samples["docker_stats"]})


def e6_alert_test(mode, rep, users):
    """Monitoring/alert validation: inject known latency, errors and auth failures."""
    recs = [f"records-{i}" for i in (1, 2)] if mode == "resilient" else ["records-1"]

    def brute():
        with httpx.Client(base_url=L.GATEWAY, timeout=5) as c:
            codes = [c.post("/api/auth/login", json={"username": "s1001", "password": f"wrong-{i}"}).status_code
                     for i in range(40)]
        return {str(k): codes.count(k) for k in set(codes)}

    mix = [(60, L.op_transcript), (20, L.op_profile), (20, L.op_timetable)]
    res = run(L.run_load(duration=110, rate=lambda t: 15, users=L.students(users), mix=mix, actions=[
        (10, "records latency 700 ms", lambda: [L.chaos(c, latency_ms=700) for c in recs]),
        (40, "records latency off, error rate 30%", lambda: [L.chaos(c, error_rate=0.3) for c in recs]),
        (70, "records faults off", lambda: [L.chaos(c) for c in recs]),
        (75, "40 failed logins", brute)], seed=rep))
    expected = {"HighLatencyP95{service=records-service}": 10, "HighErrorRate{service=records-service}": 40,
                "AuthFailureSpike{}": 75}
    checks = {}
    for key, t in expected.items():
        v = res.alerts.get(key, {})
        checks[key] = {"injected_at": t, "fired": "firing" in v,
                       "detection_s": round(v["firing"] - t, 2) if "firing" in v else None,
                       "resolved_at": v.get("resolved")}
    unexpected = [k for k, v in res.alerts.items() if "firing" in v and k.split("#")[0] not in expected]
    return finish(mode, "E6_alert_test", rep, res, 10, {"alert_checks": checks, "unexpected_alerts": unexpected})


def _token(claims, secret):
    return jwt.encode(claims, secret, algorithm="HS256")


def e7_unauthorized_access(mode, rep, users):
    """Security: protected operations without valid authorization."""
    before = L.integrity()
    s1 = users["s1001"]
    now = int(time.time())
    base_claims = {"sub": "s1001", "role": "admin", "sid": None, "iss": "university-auth", "iat": now, "exp": now + 600}
    forged = _token(base_claims, "attacker-guessed-secret-attacker-guessed-secret")
    expired = _token({**base_claims, "role": "student", "sid": 1001, "iat": now - 7200, "exp": now - 3600},
                     L.ENV["JWT_SECRET"])
    alg_none = jwt.encode(base_claims, key=None, algorithm="none") if hasattr(jwt, "encode") else ""
    other_email = "student1002@uni.test"
    cases = [
        ("no_token", "GET", "/api/students/1002", {}, None, {401}),
        ("forged_signature_admin", "GET", "/api/students/1002", {"Authorization": f"Bearer {forged}"}, None, {401}),
        ("expired_token", "GET", "/api/students/1002", {"Authorization": f"Bearer {expired}"}, None, {401}),
        ("alg_none_token", "GET", "/api/admin/audit", {"Authorization": f"Bearer {alg_none}"}, None, {401}),
        ("idor_other_student_profile", "GET", "/api/students/1002", L.auth(s1), None, {403}),
        ("idor_other_transcript", "GET", "/api/records/transcripts/1002", L.auth(s1), None, {403}),
        ("student_changes_grade", "POST", "/api/records/grades", L.auth(s1),
         {"student_id": 1001, "course_code": "CSE301", "term": "Spring-2026", "grade": "A"}, {403}),
        ("student_reads_audit_log", "GET", "/api/admin/audit", L.auth(s1), None, {403}),
        ("pay_for_other_student", "POST", "/api/payments", {**L.auth(s1), "Idempotency-Key": uuid.uuid4().hex},
         {"student_id": 1002, "amount": "1.00", "term": "Fall-2026", "method": "card"}, {403}),
        ("internal_endpoint_via_gateway", "GET", "/internal/students/1002", {}, None, {404, 403}),
    ]
    out = []
    with httpx.Client(base_url=L.GATEWAY, timeout=5) as c:
        for name, method, path, headers, body, expect in cases:
            r = c.request(method, path, headers=headers, json=body)
            exposed = r.status_code == 200 and (other_email in r.text or '"courses"' in r.text or '"items"' in r.text)
            out.append({"case": name, "request": f"{method} {path}", "status": r.status_code,
                        "expected": sorted(expect), "passed": r.status_code in expect, "data_exposed": exposed})
            L.log(f"  {name}: {r.status_code} {'PASS' if r.status_code in expect else 'FAIL'}")
    time.sleep(1)
    after = L.integrity()
    summary = {"cases": out, "passed": sum(x["passed"] for x in out), "total": len(out),
               "data_exposed": sum(x["data_exposed"] for x in out),
               "grades_changed": before["grades_checksum"] != after["grades_checksum"],
               "payments_created": after["payment_rows"] - before["payment_rows"],
               "audit_denied_events": after["audit_denied"] - before["audit_denied"], "integrity": after}
    _save_simple(mode, "E7_unauthorized_access", rep, summary)
    return summary


def e8_abuse_malformed(mode, rep, users):
    """Security/misuse: malformed input, oversized body, request burst, login brute force."""
    s1, reg = users["s1001"], users["registrar"]
    pay = lambda **kw: {"student_id": 1001, "amount": "10.00", "term": "Fall-2026", "method": "card", **kw}
    grade = lambda **kw: {"student_id": 1001, "course_code": "CSE301", "term": "Fall-2026", "grade": "A", **kw}
    cases = [
        ("negative_amount", "/api/payments", s1, pay(amount="-500.00")),
        ("huge_amount", "/api/payments", s1, pay(amount="999999999999.99")),
        ("xss_in_term", "/api/payments", s1, pay(term="<script>alert(1)</script>")),
        ("sql_injection_in_term", "/api/payments", s1, pay(term="Fall-2026'; DROP TABLE payments;--")),
        ("unknown_payment_method", "/api/payments", s1, pay(method="crypto")),
        ("wrong_type_student_id", "/api/payments", s1, pay(student_id="abc")),
        ("oversized_body_64kb", "/api/payments", s1, pay(term="A" * 65536)),
        ("invalid_grade_value", "/api/records/grades", reg, grade(grade="Z+")),
        ("sql_injection_course_code", "/api/records/grades", reg, grade(course_code="X' OR '1'='1")),
    ]
    out = []
    with httpx.Client(base_url=L.GATEWAY, timeout=10) as c:
        for name, path, u, body in cases:
            h = {**L.auth(u), "Idempotency-Key": uuid.uuid4().hex}
            try:
                r = c.post(path, headers=h, json=body)
                code = r.status_code
            except httpx.HTTPError as exc:
                code = 0
            out.append({"case": name, "status": code, "rejected": 400 <= code < 500,
                        "server_error": code >= 500 or code == 0})
            L.log(f"  {name}: {code}")
        r = c.post("/api/payments", headers={**L.auth(s1), "Content-Type": "application/json",
                                             "Idempotency-Key": uuid.uuid4().hex}, content=b"{not json")
        out.append({"case": "invalid_json", "status": r.status_code, "rejected": 400 <= r.status_code < 500,
                    "server_error": r.status_code >= 500})

    # burst from a single client while a normal user keeps working
    burst_users = [s1]
    probe_user = users["s1002"]

    async def burst():
        res = L.RunResult()
        limits = httpx.Limits(max_connections=500)
        async with httpx.AsyncClient(base_url=L.GATEWAY, timeout=5, limits=limits) as cl:
            t0 = time.perf_counter()
            tasks = []
            for i in range(400):  # ~200 rps for 2 s
                tasks.append(asyncio.create_task(L._send(cl, "GET", "/api/students/1001",
                                                         {"headers": L.auth(burst_users[0])}, "burst", t0, res.records)))
                if i % 10 == 0:
                    tasks.append(asyncio.create_task(L._send(cl, "GET", f"/api/students/{probe_user.student_id}",
                                                             {"headers": L.auth(probe_user)}, "probe", t0, res.records)))
                await asyncio.sleep(0.005)
            await asyncio.gather(*tasks)
        return res

    bres = run(burst())
    b = [r for r in bres.records if r.label == "burst"]
    p = [r for r in bres.records if r.label == "probe"]
    burst_summary = {"burst_requests": len(b), "burst_ok": sum(r.status == 200 for r in b),
                     "burst_429": sum(r.status == 429 for r in b),
                     "burst_errors": sum(r.status not in (200, 429) for r in b),
                     "probe_requests": len(p), "probe_ok": sum(r.status == 200 for r in p),
                     "probe_p95_ms": L.summarize(L.RunResult(records=p), None)["latency_p95_ms"]}
    with httpx.Client(base_url=L.GATEWAY, timeout=5) as c:
        codes = [c.post("/api/auth/login", json={"username": "admin", "password": f"guess-{i}"}).status_code
                 for i in range(100)]
    brute = {str(k): codes.count(k) for k in sorted(set(codes))}
    time.sleep(1)
    healthy = all(L.container_state(x)[0] for x in L.APP_CONTAINERS[mode])
    after = L.integrity()
    summary = {"cases": out, "rejected": sum(x["rejected"] for x in out), "total": len(out),
               "server_errors": sum(x["server_error"] for x in out), "burst": burst_summary,
               "login_bruteforce_status_counts": brute, "all_containers_running": healthy,
               "invalid_payments_stored": after["invalid_payments"], "invalid_grades_stored": after["invalid_grades"],
               "integrity": after}
    L.log(f"  burst: {burst_summary}  brute: {brute}")
    _save_simple(mode, "E8_abuse_malformed", rep, summary)
    return summary


def _pay_with_retry(client: httpx.Client, u, key: str, amount: str, attempts: int = 4):
    body = {"student_id": u.student_id, "amount": amount, "term": "Fall-2026", "method": "card"}
    for i in range(attempts):
        try:
            r = client.post("/api/payments", headers={**L.auth(u), "Idempotency-Key": key}, json=body)
            if r.status_code in (200, 201):
                return True, i + 1
            if r.status_code in (400, 401, 403, 404, 409, 422):
                return False, i + 1
        except httpx.HTTPError:
            pass
        time.sleep(0.2 * (2 ** i))
    return False, attempts


def _keeper(mode, chaos_settings, stop: threading.Event, log_events: list):
    """Re-apply chaos after restarts; in baseline also act as the operator restarting crashed instances."""
    pays = [c for c in L.APP_CONTAINERS[mode] if c.startswith("payment")]
    while not stop.is_set():
        for c in pays:
            running, _ = L.container_state(c)
            if not running and mode == "baseline":
                L.start(c)
                log_events.append({"t": time.time(), "event": "operator_restart", "container": c})
            if running and health_probe(c):
                try:
                    out = L.sh(["docker", "exec", c, "python", "-c",
                                "import os,urllib.request as u;r=u.Request('http://localhost:8000/chaos',"
                                "headers={'X-Chaos-Token':os.environ['CHAOS_TOKEN']});print(u.urlopen(r).read().decode())"],
                               check=False)
                    if out and json.loads(out) != {**json.loads(out), **chaos_settings}:
                        L.chaos(c, **chaos_settings)
                        log_events.append({"t": time.time(), "event": "chaos_reapplied", "container": c})
                except Exception:
                    pass
        time.sleep(0.5)


def e9_consistency(mode, rep, users):
    """Recovery and consistency of payments under (A) lost responses and (B) crashes mid-transaction."""
    studs = L.students(users)
    phases = {}
    for phase, settings, n in (("A_response_lost_after_commit", {"fail_after_commit_rate": 0.3}, 100),
                               ("B_crash_mid_transaction", {"crash_mid_txn_rate": 0.08}, 80)):
        L.reset_state(mode)
        for c in L.APP_CONTAINERS[mode]:
            if c.startswith("payment"):
                L.chaos(c, **settings)
        stop = threading.Event()
        kev: list = []
        keeper = threading.Thread(target=_keeper, args=(mode, settings, stop, kev), daemon=True)
        keeper.start()
        keys = [(studs[i % len(studs)], f"e9{phase[0]}-{rep}-{i}-{uuid.uuid4().hex[:8]}") for i in range(n)]
        t0 = time.time()
        with httpx.Client(base_url=L.GATEWAY, timeout=5) as client, cf.ThreadPoolExecutor(6) as ex:
            results = list(ex.map(lambda uk: (uk[1], *_pay_with_retry(client, uk[0], uk[1], "25.00")), keys))
        elapsed = time.time() - t0
        stop.set()
        keeper.join(timeout=5)
        for c in L.APP_CONTAINERS[mode]:
            if L.container_state(c)[0]:
                try:
                    L.chaos(c)
                except Exception:
                    pass
        acked = [k for k, ok, _ in results if ok]
        db_refs = set(L.psql("SELECT DISTINCT client_ref FROM payments WHERE client_ref IS NOT NULL").split())
        integ = L.integrity()
        phases[phase] = {
            "logical_payments": n, "acknowledged": len(acked), "attempts_total": sum(a for _, _, a in results),
            "lost_acknowledged": sum(1 for k in acked if k not in db_refs),
            "duplicate_rows": integ["duplicate_rows"], "payment_rows": integ["payment_rows"],
            "balance_mismatches": integ["balance_mismatches"],
            "expected_total_for_acked": f"{25 * len(acked):.2f}", "sum_payments": integ["sum_payments"],
            "sum_account_balances": integ["total_paid"],
            "restarts_or_reapplies": len(kev), "elapsed_s": round(elapsed, 1)}
        L.log(f"  {phase}: {phases[phase]}")
    _save_simple(mode, "E9_consistency", rep, phases)
    return phases


def e10_chaos_soak(mode, rep, users):
    """Reliability soak: random instance crashes (seeded) for 180 s under constant load."""
    rnd = random.Random(100 + rep)
    targets = [c for c in L.APP_CONTAINERS[mode]]
    t, schedule = 15.0, []
    while True:
        t += max(20.0, rnd.expovariate(1 / 30))
        if t > 160:
            break
        schedule.append((round(t, 1), rnd.choice(targets)))
    actions = [(at, f"crash {c}", (lambda c=c: L.crash_app(c))) for at, c in schedule]
    bg = [watch(targets)] + ([operator()] if mode == "baseline" else [])
    res = run(L.run_load(duration=180, rate=lambda t: 20, users=L.students(users), mix=L.MIX_DEFAULT,
                         actions=actions, background=bg, seed=rep))
    # per-incident downtime from container transitions
    downtimes = []
    trans = res.samples.get("transitions", [])
    for at, c in schedule:
        d = [x for x in trans if x["container"] == c and x["t"] >= at - 0.5]
        down = next((x for x in d if not x["up"]), None)
        up = next((x for x in d if x["up"] and down and x["t"] > down["t"]), None)
        downtimes.append(round(up["t"] - at, 2) if (down and up) else (0.0 if not down else None))
    measured = [x for x in downtimes if x is not None]
    extra = {"crashes": [{"t": at, "container": c, "instance_downtime_s": dt} for (at, c), dt in zip(schedule, downtimes)],
             "mttr_instance_s": round(sum(measured) / len(measured), 2) if measured else None,
             "observed_mtbf_s": round(180 / len(schedule), 1) if schedule else None}
    return finish(mode, "E10_chaos_soak", rep, res, schedule[0][0] if schedule else None, extra)


def _save_simple(mode, exp, rep, summary):
    d = L.ROOT / "results" / "raw" / mode
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{exp}_r{rep}.json").write_text(json.dumps({"mode": mode, "experiment": exp, "rep": rep,
                                                      "summary": summary}, indent=1, default=str), encoding="utf-8")


EXPERIMENTS = {
    "E1": (e1_instance_crash, 3), "E2": (e2_node_failure, 3), "E3": (e3_dependency_timeout, 3),
    "E4": (e4_database_failure, 3), "E5": (e5_load_spike, 2), "E6": (e6_alert_test, 1),
    "E7": (e7_unauthorized_access, 1), "E8": (e8_abuse_malformed, 1), "E9": (e9_consistency, 1),
    "E10": (e10_chaos_soak, 1),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "resilient"], required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--reps", type=int, default=0, help="override repetitions")
    ap.add_argument("--no-bring-up", action="store_true", help="use the already running stack")
    args = ap.parse_args()
    selected = [e.strip() for e in args.only.split(",") if e.strip()] or list(EXPERIMENTS)
    if not args.no_bring_up:
        L.bring_up(args.mode)
    users = L.login_all(args.mode)
    L.log(f"logged in {len(users)} test users")
    for name in selected:
        fn, reps = EXPERIMENTS[name]
        for rep in range(1, (args.reps or reps) + 1):
            L.log(f"=== {args.mode} {name} rep {rep}: {fn.__doc__.strip().splitlines()[0]}")
            L.reset_state(args.mode)
            try:
                fn(args.mode, rep, users)
            except Exception as exc:
                L.log(f"  !! {name} rep {rep} failed: {type(exc).__name__}: {exc}")
    L.reset_state(args.mode)
    L.log("done")


if __name__ == "__main__":
    main()
