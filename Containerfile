FROM docker.io/python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SIDEPLAY_DATA_ROOT=/var/lib/sideplay

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg redis-server \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000 7000

ENTRYPOINT ["/app/container-entrypoint.sh"]
