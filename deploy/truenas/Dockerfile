FROM python:3.12-slim
ARG SUPERCRONIC=v0.2.33
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates libgl1 libglib2.0-0 \
 && arch=$(dpkg --print-architecture) \
 && curl -fsSLo /usr/local/bin/supercronic \
      https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC}/supercronic-linux-${arch} \
 && chmod +x /usr/local/bin/supercronic \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/ scripts/
COPY deploy/truenas/crontab /app/crontab
ENV KOBO_PAPER_CONFIG=/config/config.json PYTHONUNBUFFERED=1
