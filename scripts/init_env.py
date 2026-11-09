"""Create a local .env with random secrets (never commit .env)."""
import pathlib
import secrets

root = pathlib.Path(__file__).resolve().parent.parent
env = root / ".env"
if env.exists():
    print(".env already exists - keeping it")
    raise SystemExit(0)
values = {
    "POSTGRES_PASSWORD": secrets.token_urlsafe(18),
    "STUDENT_DB_PASSWORD": secrets.token_urlsafe(18),
    "PAYMENT_DB_PASSWORD": secrets.token_urlsafe(18),
    "RECORDS_DB_PASSWORD": secrets.token_urlsafe(18),
    "TIMETABLE_DB_PASSWORD": secrets.token_urlsafe(18),
    "REPLICATION_PASSWORD": secrets.token_urlsafe(18),
    "JWT_SECRET": secrets.token_urlsafe(48),
    "SERVICE_TOKEN": secrets.token_urlsafe(32),
    "CHAOS_TOKEN": secrets.token_urlsafe(24),
    "GRAFANA_ADMIN_PASSWORD": secrets.token_urlsafe(12),
    "SEED_PASSWORD": "Test-" + secrets.token_urlsafe(9),
}
env.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
print(f"wrote {env} (test login password for admin/registrar/s1001..s1060 is SEED_PASSWORD)")
