# Cloud server image (phones + alerts + bot). Camera stations run the AI separately.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY deploy/requirements-server.txt deploy/requirements-server.txt
RUN pip install -r deploy/requirements-server.txt

COPY backend backend
COPY frontend frontend

# database, alert log and the push-notification key live here (mounted as a volume)
VOLUME /app/data
EXPOSE 8000

# No cameras on the server; every junction without a station is simulated until one connects.
CMD ["sh", "-c", "uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000}"]
