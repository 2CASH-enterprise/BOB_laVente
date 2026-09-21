#!/usr/bin/env bash
# Sauvegarde PostgreSQL (section 49 du cahier des charges).
# Usage : ./scripts/backup.sh
# À planifier via cron, ex. tous les jours à 3h :
#   0 3 * * * cd /opt/bob && ./scripts/backup.sh >> /var/log/bob-backup.log 2>&1

set -euo pipefail

BACKUP_DIR="/opt/bob/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=14

mkdir -p "$BACKUP_DIR"

docker compose exec -T db pg_dump -U bob -d bob --format=custom > "$BACKUP_DIR/bob_${TIMESTAMP}.dump"

echo "Sauvegarde créée : $BACKUP_DIR/bob_${TIMESTAMP}.dump"

# Nettoyage des sauvegardes de plus de RETENTION_DAYS jours
find "$BACKUP_DIR" -name "bob_*.dump" -mtime "+${RETENTION_DAYS}" -delete

echo "Sauvegardes de plus de ${RETENTION_DAYS} jours supprimées."
