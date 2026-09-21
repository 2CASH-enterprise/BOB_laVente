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

## HTTPS (obligatoire pour que Meta puisse joindre le webhook)

Ce projet inclut Caddy, qui obtient et renouvelle automatiquement un certificat Let's
Encrypt dès qu'un domaine valide pointe vers ce serveur (enregistrement DNS de type A).

1. Pointez votre domaine (ex. `bob.votre-entreprise.com`) vers l'IP de ce serveur.
2. Ajoutez-le dans `.env` : `DOMAIN=bob.votre-entreprise.com`
3. `docker compose up -d --build` : Caddy écoute sur les ports 80/443, obtient le
   certificat automatiquement au premier accès, et route vers l'API en interne.
4. Vérifiez : `curl https://votre-domaine/health` doit répondre `{"status":"ok"}`.

⚠️ Si un autre reverse proxy tourne déjà sur ce serveur pour d'autres projets (vérifié
lors du déploiement des sprints précédents), vérifiez qu'aucun autre service n'occupe
déjà les ports 80/443 avant de démarrer Caddy (`ss -tlnp | grep -E ":(80|443)"`).

## Sauvegardes PostgreSQL

```bash
chmod +x scripts/backup.sh scripts/restore.sh
./scripts/backup.sh   # crée un dump dans ./backups/
```

À planifier en tâche cron (tous les jours à 3h, conserve 14 jours de sauvegardes) :

```bash
crontab -e
# ajouter :
0 3 * * * cd /opt/bob && ./scripts/backup.sh >> /var/log/bob-backup.log 2>&1
```

Restauration : `./scripts/restore.sh backups/bob_20260101_030000.dump`

## Sécurité (section 32)

- Signature des webhooks WhatsApp vérifiée (HMAC-SHA256) dès que `WHATSAPP_APP_SECRET`
  est renseigné dans `.env` — à faire avant la mise en production.
- Rate limiting sur `/api/v1/auth/login` (5 tentatives/minute par email, 20/minute par IP).
- Logs d'audit (`audit_logs`) pour les actions sensibles : connexions, création de
  tenant, prise de contrôle/restitution de conversation, création de commande.
- En-têtes de sécurité HTTP (`X-Frame-Options`, `X-Content-Type-Options`, HSTS).

## Prochaine étape suggérée

Le MVP (section 44 du cahier des charges) est désormais complet côté backend et
dashboard basique. Restent, en phase 2 : intégration ERP/POS, paiement réel (section 39),
livraison (section 40), agent vocal, campagnes de relance automatisées (section 23).
