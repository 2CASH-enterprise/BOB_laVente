#!/usr/bin/env bash
# Restauration PostgreSQL à partir d'une sauvegarde (section 49).
# Usage : ./scripts/restore.sh backups/bob_20260101_030000.dump
#
# ATTENTION : écrase la base actuelle. À utiliser en connaissance de cause,
# idéalement après avoir arrêté l'API pour éviter les écritures concurrentes.

set -euo pipefail

if [ -z "${1:-}" ]; then
  echo "Usage : $0 <fichier.dump>"
  exit 1
fi

DUMP_FILE="$1"

if [ ! -f "$DUMP_FILE" ]; then
  echo "Fichier introuvable : $DUMP_FILE"
  exit 1
fi

echo "Restauration de $DUMP_FILE — la base actuelle sera écrasée."
read -p "Continuer ? (oui/non) " CONFIRM
if [ "$CONFIRM" != "oui" ]; then
  echo "Annulé."
  exit 0
fi

docker compose exec -T db pg_restore -U bob -d bob --clean --if-exists < "$DUMP_FILE"

echo "Restauration terminée."
