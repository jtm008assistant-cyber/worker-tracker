FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Etc/UTC \
    SERVICE_ACCOUNT_JSON=/data/sa-key.json

WORKDIR /app

COPY worker_tracker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY worker_tracker /app/worker_tracker

RUN mkdir -p /data

CMD ["python", "-m", "worker_tracker", "bot"]
