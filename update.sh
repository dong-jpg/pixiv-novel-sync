#!/bin/bash
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

INSTALL_DIR="${HOME}/pixiv-novel-sync"
SERVICE_NAME="pixiv-novel-sync"
FLASK_PORT=5011
NGINX_PORT=5010
BACKUP_SUFFIX="$(date +%Y%m%d_%H%M%S)"
ENV_BACKUP=".env.bak.${BACKUP_SUFFIX}"
CONFIG_BACKUP="config/config.yaml.bak.${BACKUP_SUFFIX}"
CONFIG_RESTORED=false

restore_config() {
    if [ "$CONFIG_RESTORED" = "true" ]; then
        return
    fi
    if [ -f "$ENV_BACKUP" ]; then
        cp -f "$ENV_BACKUP" .env
        echo "  已恢复 .env"
    fi
    if [ -f "$CONFIG_BACKUP" ]; then
        cp -f "$CONFIG_BACKUP" config/config.yaml
        echo "  已恢复 config.yaml"
    fi
    CONFIG_RESTORED=true
}

on_error() {
    echo -e "${RED}更新失败，正在恢复配置...${NC}"
    restore_config
    exit 1
}

trap on_error ERR

echo -e "${GREEN}=== Pixiv Novel Sync 更新脚本 ===${NC}"

cd "$INSTALL_DIR"

# 1. 备份配置
echo -e "${GREEN}[1/6] 备份配置...${NC}"
if [ -f ".env" ]; then
    cp -f .env "$ENV_BACKUP"
fi
if [ -f "config/config.yaml" ]; then
    cp -f config/config.yaml "$CONFIG_BACKUP"
fi

# 2. 拉取最新代码
echo -e "${GREEN}[2/6] 拉取最新代码...${NC}"
git fetch origin
git reset --hard origin/main

# 3. 清理旧缓存
echo -e "${GREEN}[3/6] 清理旧缓存...${NC}"
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true

# 4. 更新依赖
echo -e "${GREEN}[4/6] 更新 Python 依赖...${NC}"
source .venv/bin/activate
pip install -e . -q

# 5. 恢复配置
echo -e "${GREEN}[5/6] 恢复配置...${NC}"
restore_config

# 更新 Nginx 配置
echo -e "${GREEN}[5.5/6] 更新 Nginx 配置...${NC}"
sudo cp -f config/nginx/pixiv-novel-sync.conf /etc/nginx/sites-available/pixiv-novel-sync

# 确保缓存目录权限正确
sudo chmod -R 775 /var/cache/nginx/pixiv_img 2>/dev/null || true
sudo chmod g+s /var/cache/nginx/pixiv_img 2>/dev/null || true
sudo setfacl -d -m g::rwx /var/cache/nginx/pixiv_img 2>/dev/null || true

sudo nginx -t && sudo systemctl reload nginx
echo "  Nginx 配置已更新"

# 6. 重启服务
echo -e "${GREEN}[6/6] 重启服务...${NC}"
sudo systemctl daemon-reload
sudo systemctl restart ${SERVICE_NAME}

sleep 2

if sudo systemctl is-active --quiet ${SERVICE_NAME}; then
    echo -e "${GREEN}=== 更新成功! ===${NC}"
    echo ""
    echo "访问地址: http://$(hostname -I | awk '{print $1}'):${NGINX_PORT}"
    echo ""
    echo "缓存管理:"
    echo "  查看缓存大小: du -sh /var/cache/nginx/pixiv_img"
    echo "  清空缓存: sudo rm -rf /var/cache/nginx/pixiv_img/*"
    echo ""
    echo -e "${YELLOW}提示: 如果页面显示旧版样式，请按 Ctrl+Shift+R 强制刷新浏览器缓存${NC}"
else
    echo -e "${RED}服务启动失败，请检查日志${NC}"
    sudo journalctl -u ${SERVICE_NAME} --no-pager -n 20
fi
