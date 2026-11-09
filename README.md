# Resilient University Information System

Endterm project for **Fault Tolerance and Dependable Computing** (CSE-2505M), by Kalybekov Sultan Beibarys.

A small university platform (students, tuition payments, academic records, timetable)
built as Python/FastAPI microservices on Docker, in two configurations that are compared
experimentally:

| | Baseline (midterm scope) | Resilient (endterm) |
|---|---|---|
| Instances | 1 per service | 2 per critical service, gateway load balancing |
| Recovery | manual restart | Docker restart policy, passive health checks, failover |
| Inter-service calls | no timeout, no retry | timeouts, bounded retries with backoff, circuit breakers |
| Database | single PostgreSQL | primary + streaming hot-standby replica + periodic backups |
| Payments | no idempotency, non-atomic writes | idempotency keys, single atomic transaction |
| Degradation | none | dashboard/transcript keep working without non-critical parts |
| Security | none | JWT auth, roles, object-level checks, validation, rate limiting, audit log, least-privilege DB roles |
| Observability | metrics + JSON logs (used for measurement) | same + request IDs across services, 7 alert rules, Grafana dashboard |

## Project layout

```
services/            one Docker image, four services (student, payment, records, timetable)
  ftcommon/          shared library: config, logs, metrics, resilience, security, db, chaos
gateway/             nginx configs (baseline / resilient)
db/                  schema, synthetic seed data, roles, replica + backup scripts
monitoring/          Prometheus configs, alert rules, Grafana dashboard
scripts/             experiment harness, analysis, diagram, env generator
tests/               unit tests (run in Docker) and integration tests (run against the stack)
results/             raw results (CSV/JSON per run), tables, figures, logs
report/              report generator and the final report (.docx / .pdf)

## Results at a glance

See `results/results_tables.md` (generated) and the report. Highlights: instance crash 52.8% -> 100% successful requests
(recovery 21.0 s -> 1.9 s); database crash 34.3% -> 100%; 10/10 attacks denied; 34 -> 0 duplicate payments.
```

## Requirements

- Docker Desktop (Compose v2)
- Python 3.11+ (for tests, experiments and analysis)

## Run

```bash
python scripts/init_env.py                                   # creates .env with random secrets
docker compose -f docker-compose.resilient.yml up -d --build # or docker-compose.baseline.yml
```

| URL | What |
|---|---|
| http://localhost:18080 | API gateway |
| http://localhost:19090 | Prometheus (alerts at /alerts) |
| http://localhost:13000 | Grafana dashboard "University Information System - Resilience" |

Test logins: `admin`, `registrar`, `s1001` ... `s1060`; the password is `SEED_PASSWORD` in `.env`.

```bash
# example: log in and read your dashboard
curl -s -X POST localhost:18080/api/auth/login -H "Content-Type: application/json" \
     -d '{"username":"s1001","password":"<SEED_PASSWORD>"}'
curl -s localhost:18080/api/dashboard/1001 -H "Authorization: Bearer <token>"
```

Stop: `docker compose -f docker-compose.resilient.yml down -v`

## Tests

```bash
python -m venv .venv && .venv/Scripts/activate    # Linux/macOS: source .venv/bin/activate
pip install httpx pytest PyJWT matplotlib

# unit tests (inside the service image, same Python and dependencies)
docker build -t ftr-unit-tests tests && docker run --rm ftr-unit-tests

# integration tests (resilient stack must be running)
python -m pytest tests/integration -q
```

## Experiments

```bash
python scripts/run_experiments.py --mode baseline     # ~20 min, starts the baseline stack itself
python scripts/run_experiments.py --mode resilient    # ~25 min
python scripts/analyze.py                             # tables + figures in results/
```

Single experiment: `python scripts/run_experiments.py --mode resilient --only E3 --reps 1`

| ID | Scenario | Fault injection |
|---|---|---|
| E1 | Application instance crash | SIGKILL of the student-service process |
| E2 | Node (hardware) failure | 3 containers of "node A" removed for 20 s |
| E3 | Dependency timeout | timetable service frozen (`docker pause`) for 20 s |
| E4 | Database failure | SIGKILL of the PostgreSQL primary |
| E5 | Load spike | 20 -> 300 -> 20 requests/s (two generator containers inside the Docker network) |
| E6 | Monitoring/alert test | injected latency, error rate, failed logins |
| E7 | Unauthorized access | 10 attack cases (no/forged/expired/alg-none tokens, IDOR, privilege escalation) |
| E8 | Abusive / malformed requests | invalid input, 64 KB body, request burst, login brute force |
| E9 | Recovery and consistency | lost responses after commit; crashes inside the payment transaction |
| E10 | Chaos soak | random instance crashes for 180 s (reliability metrics) |

## Demo (3 scenarios)

```bash
docker compose -f docker-compose.resilient.yml up -d
python scripts/demo.py crash      # instance failure, users see no errors
python scripts/demo.py security   # unauthorized access is denied and audited
python scripts/demo.py degrade    # timetable down, dashboard still answers (degraded)
```

Keep Grafana (http://localhost:13000) open next to the terminal while recording.

All data is synthetic. Fault injection (`/chaos`) is enabled only in these local test
stacks and is not reachable through the gateway.
