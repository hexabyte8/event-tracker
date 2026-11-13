FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EVENT_TRACKER_CONFIG=/app/config.json \
    EVENT_TRACKER_DB=/data/events.sqlite3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 tracker \
    && useradd --uid 10001 --gid tracker --no-create-home tracker \
    && mkdir /data && chown tracker:tracker /data
COPY event_tracker ./event_tracker
COPY config.json .
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "event_tracker.web:create_app()"]
