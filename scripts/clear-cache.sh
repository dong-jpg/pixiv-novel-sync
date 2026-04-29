#!/bin/bash
# 清理 Nginx 图片缓存

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

CACHE_DIR="/var/cache/nginx/pixiv_img"

if [ ! -d "$CACHE_DIR" ]; then
    echo -e "${YELLOW}缓存目录不存在: $CACHE_DIR${NC}"
    exit 0
fi

SIZE=$(du -sh "$CACHE_DIR" 2>/dev/null | cut -f1)
echo -e "${GREEN}当前缓存大小: $SIZE${NC}"

if [ "$1" == "--force" ] || [ "$1" == "-f" ]; then
    sudo rm -rf "${CACHE_DIR:?}"/*
    echo -e "${GREEN}缓存已清空${NC}"
else
    read -p "确认清空缓存? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        sudo rm -rf "${CACHE_DIR:?}"/*
        echo -e "${GREEN}缓存已清空${NC}"
    else
        echo "已取消"
    fi
fi
