import os
import sys

# test configuration (set before any service module is imported)
os.environ.setdefault("FT_MODE", "resilient")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-unit-test-secret-0123456789")
os.environ.setdefault("SERVICE_TOKEN", "unit-test-service-token")
os.environ.setdefault("DB_PRIMARY_DSN", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("SERVICE_NAME", "unit-tests")

sys.path.insert(0, "/app")
