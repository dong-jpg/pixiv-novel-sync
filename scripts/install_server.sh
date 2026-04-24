#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/pixiv-novel-sync/app
PYTHON_BIN=python3
SERVICE_DIR=/etc/systemd/system

sudo useradd --system --home /opt/pixiv-novel-sync --shell /usr/sbin/nologin pixivsync 2>/dev/null || true
sudo mkdir -p "$APP_DIR"
sudo chown -R "$USER":"$USER" /opt/pixiv-novel-sync

$PYTHON_BIN -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install .

mkdir -p data/state data/library/public data/library/private
cp -n .env.example .env || true
cp -n config/config.yaml.example config/config.yaml || true

sudo cp deploy/systemd/pixiv-novel-sync.service "$SERVICE_DIR/"
sudo cp deploy/systemd/pixiv-novel-sync.timer "$SERVICE_DIR/"
sudo systemctl daemon-reload
sudo systemctl enable --now pixiv-novel-sync.timer
