#!/usr/bin/env bash
# Полный бэкап SvoyVPN (код + .env + git, без venv).
set -euo pipefail
BACKUP_DIR="${BACKUP_DIR:-/root/backups/SvoyVPN}"
TS=$(date +%Y%m%d_%H%M%S)
ARCHIVE="$BACKUP_DIR/SvoyVPN_${TS}.tar.gz"
mkdir -p "$BACKUP_DIR"
tar -czf "$ARCHIVE" \
  --exclude='SvoyVPN/venv' \
  --exclude='SvoyVPN/**/__pycache__' \
  --exclude='SvoyVPN/**/*.pyc' \
  --exclude='SvoyVPN/node_modules' \
  --exclude='SvoyVPN/.mypy_cache' \
  --exclude='SvoyVPN/.pytest_cache' \
  -C /root SvoyVPN
ln -sfn "$ARCHIVE" "$BACKUP_DIR/SvoyVPN_latest.tar.gz"
# Храним последние 14 архивов
ls -1t "$BACKUP_DIR"/SvoyVPN_2*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "Backup: $ARCHIVE"
echo "Latest: $BACKUP_DIR/SvoyVPN_latest.tar.gz"
