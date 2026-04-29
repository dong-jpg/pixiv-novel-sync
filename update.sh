#!/bin/bash
set -e

cd ~/pixiv-novel-sync

echo "备份配置文件..."
mv config/config.yaml config/config.yaml.local

echo "拉取最新代码..."
git pull origin main

echo "恢复配置文件..."
mv config/config.yaml.local config/config.yaml

echo "重启服务..."
sudo systemctl restart pixiv-novel-sync

echo "更新完成！"
