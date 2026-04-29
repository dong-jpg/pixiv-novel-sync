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
PORT="${PORT:-5010}"

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
echo -e "${GREEN}[1/7] 安装系统依赖...${NC}"
sudo apt update -qq
sudo apt install -y -qq python3 python3-pip python3-venv git > /dev/null 2>&1

# 2. 克隆或更新代码
echo -e "${GREEN}[2/7] 获取最新代码...${NC}"
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
echo -e "${GREEN}[3/7] 清理旧缓存...${NC}"
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
find . -type f -name "*.pyo" -delete 2>/dev/null || true

# 4. 创建虚拟环境并安装依赖
echo -e "${GREEN}[4/7] 安装 Python 依赖...${NC}"
python3 -m venv --clear .venv
source .venv/bin/activate
pip install --upgrade pip -q
pip install -e . -q

# 5. 恢复或创建配置文件
echo -e "${GREEN}[5/7] 配置文件...${NC}"
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

# 6. 创建 systemd 服务
echo -e "${GREEN}[6/7] 配置系统服务...${NC}"
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Pixiv Novel Sync Web UI
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${INSTALL_DIR}
Environment=PATH=${INSTALL_DIR}/.venv/bin
ExecStart=${INSTALL_DIR}/.venv/bin/pixiv-novel-sync --config config/config.yaml web-token-ui --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME} > /dev/null 2>&1
sudo systemctl restart ${SERVICE_NAME}

# 7. 等待服务启动
echo -e "${GREEN}[7/7] 启动服务...${NC}"
sleep 2

# 检查服务状态
if sudo systemctl is-active --quiet ${SERVICE_NAME}; then
    echo -e "${GREEN}=== 部署成功! ===${NC}"
    echo ""
    echo "访问地址: http://$(hostname -I | awk '{print $1}'):${PORT}"
    echo ""
    echo "常用命令:"
    echo "  查看状态: sudo systemctl status ${SERVICE_NAME}"
    echo "  查看日志: sudo journalctl -u ${SERVICE_NAME} -f"
    echo "  重启服务: sudo systemctl restart ${SERVICE_NAME}"
    echo "  编辑配置: nano ${INSTALL_DIR}/.env"
    echo ""
    echo -e "${YELLOW}提示: 如果页面显示旧版样式，请按 Ctrl+Shift+R (或 Cmd+Shift+R) 强制刷新浏览器缓存${NC}"
else
    echo -e "${RED}服务启动失败，请检查日志:${NC}"
    sudo journalctl -u ${SERVICE_NAME} --no-pager -n 20
fi
