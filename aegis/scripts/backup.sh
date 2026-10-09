#!/usr/bin/env bash
# Create and verify a backup now (safe while running; uses SQLite's online backup API).
set -euo pipefail
sudo -u aegis AEGIS_ENV_FILE=/etc/aegis/aegis.env /opt/aegis/.venv/bin/aegis backup
