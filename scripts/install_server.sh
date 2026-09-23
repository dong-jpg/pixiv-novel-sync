#!/usr/bin/env bash
set -euo pipefail

# 历史/高级入口：本脚本只安装旧的 systemd timer 同步任务。
# Web 部署请使用仓库根目录 deploy.sh；不要将本脚本作为 Web 部署入口。

APP_DIR=/opt/pixiv-novel-sync/app
PYTHON_BIN=python3
SERVICE_DIR=/etc/systemd/system

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

sudo useradd --system --home /opt/pixiv-novel-sync --shell /usr/sbin/nologin pixivsync 2>/dev/null || true
sudo mkdir -p "$APP_DIR"
sudo chown -R "$USER":"$USER" /opt/pixiv-novel-sync

# 单元文件的 WorkingDirectory 是 APP_DIR。在别的目录里建 venv 的话，定时任务找不到解释器。
if [ "$SOURCE_DIR" != "$APP_DIR" ]; then
  tar -C "$SOURCE_DIR" \
    --exclude .venv --exclude data --exclude .backup --exclude .git \
    -cf - . | tar -C "$APP_DIR" -xf -
fi

cd "$APP_DIR"
$PYTHON_BIN -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install .

mkdir -p data/state data/library/public data/library/private
cp -n .env.example .env || true
cp -n config/config.yaml.example config/config.yaml || true

# 服务以 pixivsync 运行，库和虚拟环境必须归它，否则写不了 data/。
sudo chown -R pixivsync:pixivsync /opt/pixiv-novel-sync

sudo cp deploy/systemd/pixiv-novel-sync.service "$SERVICE_DIR/"
sudo cp deploy/systemd/pixiv-novel-sync.timer "$SERVICE_DIR/"
sudo systemctl daemon-reload
sudo systemctl enable --now pixiv-novel-sync.timer
