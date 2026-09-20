# Bob — Sprint 1 : Infrastructure

Squelette d'infrastructure du SaaS multi-tenant « agent vendeur IA connecté à WhatsApp »,
conforme au cahier des charges (sections 3, 4, 30, 31, 36, 38, 48).

## Contenu de ce Sprint 1

- FastAPI (async) + SQLAlchemy 2.x (async) + Pydantic 2.x
- Modèles `Tenant` et `User` (avec rôles RBAC : OWNER / ADMIN / MANAGER / AGENT / VIEWER)
- Authentification JWT (inscription d'une entreprise + connexion)
- **Isolation multi-tenant** : `TenantScopedRepository` garantit qu'aucune requête ne peut lire
  les données d'un autre tenant — testé explicitement (`app/tests/test_tenant_isolation.py`)
- Docker Compose : FastAPI + PostgreSQL 16 + Redis 7
- Alembic configuré pour les migrations
- Suite de tests (pytest + SQLite en mémoire) : **7/7 tests passent**

## Ce qui n'est PAS encore dans ce Sprint 1

Catalogue, WhatsApp, agent IA, commandes, dashboard, négociation, etc. — prévus aux sprints
suivants (section 48 du cahier des charges).

## Démarrage local (sur le serveur, après extraction de l'archive)

```bash
cp .env.example .env
# éditer .env : générer un SECRET_KEY avec
#   python3 -c "import secrets; print(secrets.token_urlsafe(64))"

docker compose up -d --build

# Au premier lancement (schéma vide) : générer et appliquer la première migration
docker compose exec api alembic revision --autogenerate -m "initial schema"
docker compose exec api alembic upgrade head
```

L'API est ensuite disponible sur `http://<serveur>:8000`, documentation interactive sur
`http://<serveur>:8000/docs`.

## Tester avant déploiement (déjà fait ici, à refaire si vous modifiez le code)

```bash
pip install -r requirements.txt
SECRET_KEY=test DATABASE_URL="sqlite+aiosqlite:///:memory:" pytest app/tests -v
```

## Point d'attention migrations (section 58.4)

- Une **nouvelle table** (ex. `products`, `orders` aux prochains sprints) est créée automatiquement
  par SQLAlchemy au premier `create_all`, mais en production le schéma est piloté par Alembic :
  `alembic revision --autogenerate` détecte les nouveaux modèles.
- Une **nouvelle colonne sur une table existante** nécessite systématiquement une migration Alembic
  explicite (`alembic revision --autogenerate -m "..."` puis relecture du script généré avant
  `alembic upgrade head`) — jamais de `ALTER TABLE` manuel non versionné.

## Prochaine étape suggérée

Sprint 2 (section 48) : intégration WhatsApp — webhook `POST /webhooks/whatsapp`, modèle
`whatsapp_accounts` (section 59.4), flux Embedded Signup.
