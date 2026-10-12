FROM python:3.12-slim

WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

EXPOSE 8000

# Lot 70 — vraie adresse du visiteur (limites d'essais par adresse) : uvicorn lit X-Forwarded-For posé par nginx.
# On ne fait confiance qu'à nginx, qui arrive par le réseau Docker (172.16.0.0/12) ou la machine elle-même ; le port
# 8002 n'écoute que sur 127.0.0.1, donc personne d'autre ne peut se faire passer pour nginx.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,172.16.0.0/12"]
