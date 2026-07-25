# Iraqi 6th-grade results service — API + Telegram bot share one image.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: RAR extractors for ministry .rar uploads. bsdtar (libarchive-tools)
# handles RAR3/5 reliably; unar is a fallback.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libarchive-tools unar \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persisted SQLite lives here (mounted as a volume in docker-compose).
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 8000

# Default: run the API. The bot service overrides CMD in docker-compose.
CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
