FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend
COPY config.yaml /app/config.yaml

ENV PYTHONPATH=/app/backend \
    MAC_CONFIG_PATH=/app/config.yaml \
    MAC_HOST=0.0.0.0 \
    MAC_PORT=5555

EXPOSE 5555

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5555", "--app-dir", "/app/backend"]
