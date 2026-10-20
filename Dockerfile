FROM python:3.12-slim

WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Lot 87 — modèle de détourage (IS-Net « isnet-general-use », licence Apache 2.0) intégré à l'image, vérifié par son
# empreinte : jamais téléchargé pendant que Bob sert les clients.
RUN mkdir -p /opt/models \
    && python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx', '/opt/models/isnet-general-use.onnx')" \
    && echo "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a  /opt/models/isnet-general-use.onnx" | sha256sum -c -

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

# Lot 73 — les conteneurs ne tournent plus en « root » : un utilisateur sans droits (uid 1000).
# Seul app/static/uploads doit lui appartenir sur le serveur (photos de produits) :
#   chown -R 1000:1000 /opt/bob/app/static/uploads
# Lot 88 — et app/var (PDF en attente de lecture, jamais servis au public) :
#   mkdir -p /opt/bob/app/var/catalog-imports && chown -R 1000:1000 /opt/bob/app/var
# Pas de fichiers .pyc écrits dans le dossier app monté depuis le serveur.
ENV PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 1000 bob && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin bob
USER bob

EXPOSE 8000

# Lot 70 — vraie adresse du visiteur (limites d'essais par adresse) : uvicorn lit X-Forwarded-For posé par nginx.
# On ne fait confiance qu'à nginx, qui arrive par le réseau Docker (172.16.0.0/12) ou la machine elle-même ; le port
# 8002 n'écoute que sur 127.0.0.1, donc personne d'autre ne peut se faire passer pour nginx.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,172.16.0.0/12"]
