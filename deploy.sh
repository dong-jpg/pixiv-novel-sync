#!/bin/bash
set -e

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=== Pixiv Novel Sync 部署脚本 ===${NC}"

# 配置
REPO_URL="https://github.com/dong-jpg/pixiv-novel-sync.git"
INSTALL_DIR="${HOME}/pixiv-novel-sync"
SERVICE_NAME="pixiv-novel-sync"
FLASK_PORT=5011       # Flask 内部端口
NGINX_PORT=5010       # Nginx 对外端口（用户访问）

# 检测是否已安装
if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}检测到已有安装目录: $INSTALL_DIR${NC}"
    read -p "是否更新并重新部署? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "已取消"
        exit 0
    fi
fi

# 1. 安装系统依赖
echo -e "${GREEN}[1/8] 安装系统依赖...${NC}"
sudo apt update -qq
sudo apt install -y -qq python3 python3-pip python3-venv git nginx > /dev/null 2>&1

# 2. 克隆或更新代码
echo -e "${GREEN}[2/8] 获取最新代码...${NC}"
if [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR"
    # 备份配置文件
    cp -f .env .env.bak 2>/dev/null || true
    cp -f config/config.yaml config/config.yaml.bak 2>/dev/null || true

    # 拉取最新代码
    git fetch origin
    git reset --hard origin/main
else
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 3. 清理旧的 Python 缓存
echo -e "${GREEN}[3/8] 清理旧缓存...${NC}"
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
find . -type f -name "*.pyo" -delete 2>/dev/null || true

# 4. 创建虚拟环境并安装依赖
echo -e "${GREEN}[4/8] 安装 Python 依赖...${NC}"
python3 -m venv --clear .venv
source .venv/bin/activate
pip install --upgrade pip -q
pip install -e . -q

# 5. 恢复或创建配置文件
echo -e "${GREEN}[5/8] 配置文件...${NC}"
if [ -f ".env.bak" ]; then
    mv .env.bak .env
    echo "  已恢复 .env 配置"
else
    cp .env.example .env
    echo -e "  ${YELLOW}请编辑 $INSTALL_DIR/.env 填入你的 Pixiv token${NC}"
fi

if [ -f "config/config.yaml.bak" ]; then
    mv config/config.yaml.bak config/config.yaml
    echo "  已恢复 config.yaml 配置"
else
    cp config/config.yaml.example config/config.yaml
fi

# 创建数据目录
mkdir -p data/state data/library/public data/library/private

# 6. 配置 Nginx
echo -e "${GREEN}[6/8] 配置 Nginx 缓存...${NC}"

# 创建缓存目录
sudo mkdir -p /var/cache/nginx/pixiv_img
sudo chown -R www-data:www-data /var/cache/nginx/pixiv_img
sudo chmod -R 775 /var/cache/nginx/pixiv_img
# 设置 setgid 位，新创建的目录继承组权限
sudo chmod g+s /var/cache/nginx/pixiv_img
# 将当前用户加入 www-data 组
sudo usermod -a -G www-data $(whoami)
# 设置 ACL，让 www-data 和当前用户都有读写权限
sudo setfacl -d -m g::rwx /var/cache/nginx/pixiv_img
sudo setfacl -m g::rwx /var/cache/nginx/pixiv_img

# 复制 Nginx 配置
sudo cp -f config/nginx/pixiv-novel-sync.conf /etc/nginx/sites-available/pixiv-novel-sync
sudo ln -sf /etc/nginx/sites-available/pixiv-novel-sync /etc/nginx/sites-enabled/pixiv-novel-sync

# 删除默认配置（避免冲突）
sudo rm -f /etc/nginx/sites-enabled/default

# 测试并重启 Nginx
sudo nginx -t && sudo systemctl restart nginx
echo "  Nginx 配置完成，监听端口: ${NGINX_PORT}"

# 7. 创建 systemd 服务
echo -e "${GREEN}[7/8] 配置系统服务...${NC}"
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Pixiv Novel Sync Web UI
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${INSTALL_DIR}
Environment=PATH=${INSTALL_DIR}/.venv/bin
ExecStart=${INSTALL_DIR}/.venv/bin/pixiv-novel-sync --config config/config.yaml web-token-ui --host 127.0.0.1 --port ${FLASK_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME} > /dev/null 2>&1
sudo systemctl restart ${SERVICE_NAME}

# 8. 等待服务启动
echo -e "${GREEN}[8/8] 启动服务...${NC}"
sleep 2

# 检查服务状态
if sudo systemctl is-active --quiet ${SERVICE_NAME} && sudo systemctl is-active --quiet nginx; then
    echo -e "${GREEN}=== 部署成功! ===${NC}"
    echo ""
    echo "访问地址: http://$(hostname -I | awk '{print $1}'):${NGINX_PORT}"
    echo ""
    echo "架构说明:"
    echo "  Nginx (端口 ${NGINX_PORT}) → Flask (端口 ${FLASK_PORT})"
    echo "  图片请求会被 Nginx 缓存到 /var/cache/nginx/pixiv_img"
    echo ""
    echo "常用命令:"
    echo "  查看状态: sudo systemctl status ${SERVICE_NAME}"
    echo "  查看日志: sudo journalctl -u ${SERVICE_NAME} -f"
    echo "  重启服务: sudo systemctl restart ${SERVICE_NAME}"
    echo "  编辑配置: nano ${INSTALL_DIR}/.env"
    echo "  查看缓存: du -sh /var/cache/nginx/pixiv_img"
    echo "  清空缓存: sudo rm -rf /var/cache/nginx/pixiv_img/*"
    echo ""
    echo -e "${YELLOW}提示: 如果页面显示旧版样式，请按 Ctrl+Shift+R (或 Cmd+Shift+R) 强制刷新浏览器缓存${NC}"
else
    echo -e "${RED}服务启动失败，请检查日志:${NC}"
    sudo journalctl -u ${SERVICE_NAME} --no-pager -n 20
    sudo nginx -t 2>&1 || true
fi
