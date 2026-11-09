"""Runtime configuration.

FT_MODE selects the configuration that is compared in the experiments:
  baseline  - midterm-scope system: single instances, no timeouts/retries/circuit
              breakers, no idempotency, no authentication, no business validation.
  resilient - endterm system: all resilience, observability and security controls on.
"""
import os
import socket


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def secret(name: str, default: str | None = None) -> str | None:
    """Read a secret from NAME_FILE (Docker secrets style) or from NAME."""
    file_path = os.getenv(f"{name}_FILE")
    if file_path and os.path.exists(file_path):
        with open(file_path, encoding="utf-8") as fh:
            return fh.read().strip()
    return os.getenv(name, default)


MODE = os.getenv("FT_MODE", "resilient").strip().lower()
RESILIENT = MODE == "resilient"

SERVICE_NAME = os.getenv("SERVICE_NAME", "unknown-service")
INSTANCE_ID = os.getenv("INSTANCE_ID", socket.gethostname())

DB_PRIMARY_DSN = secret("DB_PRIMARY_DSN", "")
DB_REPLICA_DSN = secret("DB_REPLICA_DSN", "") if RESILIENT else ""

INTERNAL_GATEWAY = os.getenv("INTERNAL_GATEWAY", "http://gateway:8081")

# Security
AUTH_ENABLED = RESILIENT
VALIDATION_STRICT = RESILIENT
JWT_SECRET = secret("JWT_SECRET")
JWT_ISSUER = "university-auth"
JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", "3600"))
SERVICE_TOKEN = secret("SERVICE_TOKEN")

# Fault injection (test environments only)
CHAOS_ENABLED = _bool("CHAOS_ENABLED", False)
CHAOS_TOKEN = secret("CHAOS_TOKEN")

# Resilience parameters (only applied in resilient mode)
HTTP_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "1.0"))
HTTP_CONNECT_TIMEOUT_S = float(os.getenv("HTTP_CONNECT_TIMEOUT_S", "0.5"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BASE_DELAY_S = float(os.getenv("RETRY_BASE_DELAY_S", "0.1"))
CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
CB_RESET_TIMEOUT_S = float(os.getenv("CB_RESET_TIMEOUT_S", "5.0"))
DB_POOL_TIMEOUT_S = 2.0 if RESILIENT else 30.0
DB_STATEMENT_TIMEOUT_MS = 3000


def validate_config() -> None:
    """Fail fast on unsafe configuration (safe-failure principle)."""
    if RESILIENT:
        missing = [n for n, v in {"JWT_SECRET": JWT_SECRET, "SERVICE_TOKEN": SERVICE_TOKEN}.items() if not v]
        if missing:
            raise RuntimeError(f"missing required secrets: {', '.join(missing)}")
        if JWT_SECRET and len(JWT_SECRET) < 32:
            raise RuntimeError("JWT_SECRET must be at least 32 characters")
    if not DB_PRIMARY_DSN:
        raise RuntimeError("DB_PRIMARY_DSN is not configured")
