# Runs the unit tests inside the service image (same Python and dependencies).
#   docker build -t ftr-unit-tests tests && docker run --rm ftr-unit-tests
FROM ftr-university-service:latest
USER root
RUN pip install --no-cache-dir pytest==8.3.4
COPY unit /app/tests/unit
USER appuser
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "/app/tests/unit"]
