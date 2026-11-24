FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MUSIC_DIR=/music \
    DATA_DIR=/config \
    PORT=8687

WORKDIR /app
# fpcalc (Chromaprint) for audio fingerprints: finds the same song in different files
RUN apt-get update && apt-get install -y --no-install-recommends libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY main.py .

RUN mkdir -p /config /music && chmod 777 /config
VOLUME ["/config"]
EXPOSE 8687

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8687\")}/healthz', timeout=4)" || exit 1

CMD ["python", "main.py"]
