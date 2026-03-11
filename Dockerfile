FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

RUN pip install --upgrade pip && pip install -r requirements.txt

EXPOSE 8080

CMD ["sh", "-c", "gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT:-8080} app:app"]
