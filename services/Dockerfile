# One image for all four services; SERVICE selects the module to run.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# least privilege: run as an unprivileged user
RUN useradd --uid 10001 --no-create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn ${SERVICE}.main:app --host 0.0.0.0 --port 8000 --no-access-log"]
