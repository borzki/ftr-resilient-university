"""Live demonstration scenarios (run against the resilient stack).

    python scripts/demo.py crash      # service instance failure and automatic recovery
    python scripts/demo.py security   # unauthorized access is denied and audited
    python scripts/demo.py degrade    # timetable outage -> graceful degradation
"""
import sys
import threading
import time

import httpx
import jwt

import ftlab as L

C = httpx.Client(base_url=L.GATEWAY, timeout=6)


def login(user):
    r = C.post("/api/auth/login", json={"username": user, "password": L.ENV["SEED_PASSWORD"]})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def banner(text):
    print("\n" + "=" * 70 + f"\n {text}\n" + "=" * 70)


def crash():
    banner("Scenario 1: student-service instance crash (SIGKILL) under traffic")
    h = login("s1001")
    threading.Timer(4, lambda: (print("\n>>> injecting fault: kill -9 student-service on student-1\n"),
                                L.crash_app("student-1"))).start()
    ok = fail = 0
    t0 = time.time()
    while time.time() - t0 < 16:
        t = time.time() - t0
        try:
            r = C.get("/api/students/1001", headers=h)
            ok += r.status_code == 200
            fail += r.status_code != 200
            print(f"t={t:5.1f}s  HTTP {r.status_code}  served by {r.headers.get('X-Served-By', '?'):10s} "
                  f"request_id={r.headers.get('X-Request-ID', '')[:12]}")
        except httpx.HTTPError as exc:
            fail += 1
            print(f"t={t:5.1f}s  ERROR {type(exc).__name__}")
        time.sleep(0.25)
    running, health = L.container_state("student-1")
    print(f"\nresult: {ok} successful, {fail} failed requests; student-1 running again: {running} ({health})")
    print("Grafana: 'Instance availability' shows the short dip; Prometheus keeps the event history.")


def security():
    banner("Scenario 2: unauthorized and abusive access")
    s1 = login("s1001")
    forged = jwt.encode({"sub": "hacker", "role": "admin", "iss": "university-auth", "iat": int(time.time()),
                         "exp": int(time.time()) + 600}, "guessed-secret-guessed-secret-guessed", algorithm="HS256")
    cases = [("no token", "GET", "/api/students/1002", {}, None),
             ("forged admin token", "GET", "/api/admin/audit", {"Authorization": f"Bearer {forged}"}, None),
             ("student reads another student's transcript", "GET", "/api/records/transcripts/1002", s1, None),
             ("student tries to change own grade", "POST", "/api/records/grades", s1,
              {"student_id": 1001, "course_code": "CSE301", "term": "Spring-2026", "grade": "A"}),
             ("internal endpoint from outside", "GET", "/internal/students/1002", {}, None)]
    for name, m, path, h, body in cases:
        r = C.request(m, path, headers=h, json=body)
        print(f"{name:45s} -> HTTP {r.status_code}  {r.text[:60]}")
    print("\nburst of 120 requests with one token:")
    codes = [C.get("/api/students/1001", headers=s1).status_code for _ in range(120)]
    print({c: codes.count(c) for c in sorted(set(codes))}, "(429 = rate limited)")
    admin = login("admin")
    print("\nlatest audit events (admin view):")
    for e in C.get("/api/admin/audit", params={"limit": 6}, headers=admin).json()["items"]:
        print(f"  {e['ts'][11:19]}  {e['service']:16s} {e['action']:14s} {e['outcome']:8s} {e['resource']}")


def degrade():
    banner("Scenario 3: timetable service hangs -> dashboard degrades gracefully")
    h = login("s1001")

    def show(label):
        t = time.perf_counter()
        r = C.get("/api/dashboard/1001", headers=h)
        d = r.json()
        print(f"{label:22s} HTTP {r.status_code} in {(time.perf_counter() - t) * 1000:6.0f} ms  "
              f"timetable={'present' if d.get('timetable') else 'missing'}  degraded={d.get('degraded')}")

    show("normal")
    print("\n>>> docker pause timetable-1\n")
    L.sh(["docker", "pause", "timetable-1"])
    try:
        for i in range(8):
            show(f"during outage #{i + 1}")
            time.sleep(0.5)
    finally:
        print("\n>>> docker unpause timetable-1\n")
        L.sh(["docker", "unpause", "timetable-1"])
    time.sleep(6)  # circuit breaker half-open probe after its reset timeout
    for i in range(3):
        show(f"after recovery #{i + 1}")
        time.sleep(0.5)
    print("\nThe circuit breaker opened after 3 timeouts, so later requests skip the dead dependency instantly.")


if __name__ == "__main__":
    {"crash": crash, "security": security, "degrade": degrade}.get(sys.argv[1] if len(sys.argv) > 1 else "",
                                                                    lambda: print(__doc__))()
