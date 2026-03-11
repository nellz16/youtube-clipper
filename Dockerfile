FROM node:20-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    ffmpeg \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN python3 -m pip install --break-system-packages --upgrade pip && \
    python3 -m pip install --break-system-packages -r /app/requirements.txt

# Install bgutil provider source in the default home location for script mode
# Plugin docs say default script location under ~/bgutil-ytdlp-pot-provider can be used "like normal"
RUN git clone --single-branch --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /root/bgutil-ytdlp-pot-provider && \
    cd /root/bgutil-ytdlp-pot-provider/server && \
    npm ci && \
    npx tsc

COPY . /app

EXPOSE 8080

CMD ["sh", "-c", "python3 -m gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT:-8080} app:app"]
