FROM python:3.12-slim

# Prevent .pyc files and enable unbuffered stdout (better for Docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py storage.py monitor.py bot.py main.py ./
RUN mkdir -p /app/data && chown -R app:app /app

# SQLite database lives here; mount a host volume to persist across restarts
VOLUME ["/app/data"]

USER app

CMD ["python", "main.py"]
