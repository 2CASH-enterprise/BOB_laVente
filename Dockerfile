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

# Lot 73 — les conteneurs ne tournent plus en « root » : un utilisateur sans droits (uid 1000).
# Seul app/static/uploads doit lui appartenir sur le serveur (photos de produits) :
#   chown -R 1000:1000 /opt/bob/app/static/uploads
# Pas de fichiers .pyc écrits dans le dossier app monté depuis le serveur.
ENV PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 1000 bob && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin bob
USER bob

EXPOSE 8000

# Lot 70 — vraie adresse du visiteur (limites d'essais par adresse) : uvicorn lit X-Forwarded-For posé par nginx.
# On ne fait confiance qu'à nginx, qui arrive par le réseau Docker (172.16.0.0/12) ou la machine elle-même ; le port
# 8002 n'écoute que sur 127.0.0.1, donc personne d'autre ne peut se faire passer pour nginx.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,172.16.0.0/12"]
