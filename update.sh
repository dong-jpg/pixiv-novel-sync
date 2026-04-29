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

echo -e "${GREEN}=== Pixiv Novel Sync 更新脚本 ===${NC}"

cd "$INSTALL_DIR"

# 1. 备份配置
echo -e "${GREEN}[1/6] 备份配置...${NC}"
cp -f .env .env.bak 2>/dev/null || true
cp -f config/config.yaml config/config.yaml.bak 2>/dev/null || true

# 2. 拉取最新代码
echo -e "${GREEN}[2/6] 拉取最新代码...${NC}"
git fetch origin
git reset --hard origin/main

# 3. 清理缓存
echo -e "${GREEN}[3/6] 清理旧缓存...${NC}"
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true

# 4. 更新依赖
echo -e "${GREEN}[4/6] 更新 Python 依赖...${NC}"
source .venv/bin/activate
pip install -e . -q

# 5. 恢复配置
echo -e "${GREEN}[5/6] 恢复配置...${NC}"
if [ -f ".env.bak" ]; then
    mv .env.bak .env
    echo "  已恢复 .env"
fi
if [ -f "config/config.yaml.bak" ]; then
    mv config/config.yaml.bak config/config.yaml
    echo "  已恢复 config.yaml"
fi

# 更新 Nginx 配置
echo -e "${GREEN}[5.5/6] 更新 Nginx 配置...${NC}"
sudo cp -f config/nginx/pixiv-novel-sync.conf /etc/nginx/sites-available/pixiv-novel-sync
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
    echo -e "${RED}服务启动失败，请检查日志:${NC}"
    sudo journalctl -u ${SERVICE_NAME} --no-pager -n 20
fi
