# Cloudflare HTTPS 部署实施计划

> **供自动化执行者使用：**必须使用 `executing-plans`，逐项执行并在每个检查点验证结果。

**目标：**为 `pixiv.dongboapp.com` 配置可持久化的 Cloudflare `Full (strict)` HTTPS，并保持应用仅监听本机端口。

**架构：**Cloudflare 代理对外提供 HTTPS，源站 `Nginx` 使用 Cloudflare Origin CA 证书监听 `443`，并将 `80` 永久重定向到 HTTPS。源站私钥直接在服务器生成且永不离开服务器，Flask 服务继续只监听 `127.0.0.1:5011`。

**技术栈：**Cloudflare DNS、Cloudflare Origin CA、OpenSSL、Nginx、systemd、Bash

## 全局约束

- 对外域名固定为 `pixiv.dongboapp.com`。
- Cloudflare DNS 使用橙色云朵代理，源站地址为 `168.107.30.164`。
- Cloudflare 加密模式最终必须为 `Full (strict)`。
- 私钥保存到 `/etc/ssl/private/pixiv.dongboapp.com.key`，权限为 `0600`，不得写入仓库、日志或对话。
- 源站证书保存到 `/etc/ssl/certs/pixiv.dongboapp.com.pem`。
- 应用端口 `5011` 只能监听 `127.0.0.1`，不得对公网开放。
- 用户已发到对话中的旧私钥视为泄露，不得安装；对应证书必须在 Cloudflare 撤销。

---

### 任务 1：在源站生成私钥和 CSR

**文件：**

- 创建：`/etc/ssl/private/pixiv.dongboapp.com.key`
- 创建：`/home/ubuntu/pixiv.dongboapp.com.csr`

**接口：**

- 产出：仅包含公钥和域名信息、可安全提交给 Cloudflare 的 PEM 格式 CSR。

- [ ] **步骤 1：确认目标文件尚不存在**

运行：

```bash
sudo test ! -e /etc/ssl/private/pixiv.dongboapp.com.key
sudo test ! -e /etc/ssl/certs/pixiv.dongboapp.com.pem
```

预期：两条命令退出码均为 `0`。

- [ ] **步骤 2：生成 RSA 私钥和 CSR**

运行：

```bash
sudo openssl req -new -newkey rsa:2048 -nodes \
  -keyout /etc/ssl/private/pixiv.dongboapp.com.key \
  -out /home/ubuntu/pixiv.dongboapp.com.csr \
  -subj "/CN=pixiv.dongboapp.com" \
  -addext "subjectAltName=DNS:pixiv.dongboapp.com"
sudo chown root:root /etc/ssl/private/pixiv.dongboapp.com.key
sudo chmod 0600 /etc/ssl/private/pixiv.dongboapp.com.key
sudo chown ubuntu:ubuntu /home/ubuntu/pixiv.dongboapp.com.csr
sudo chmod 0644 /home/ubuntu/pixiv.dongboapp.com.csr
```

- [ ] **步骤 3：验证私钥权限、CSR 签名和域名**

运行：

```bash
sudo stat -c '%a %U %G %n' /etc/ssl/private/pixiv.dongboapp.com.key
openssl req -in /home/ubuntu/pixiv.dongboapp.com.csr -noout -verify -subject
openssl req -in /home/ubuntu/pixiv.dongboapp.com.csr -noout -text | grep -A1 'Subject Alternative Name'
```

预期：私钥为 `600 root root`，CSR 自签名验证成功，主题和 SAN 均为 `pixiv.dongboapp.com`。

### 任务 2：使用 CSR 签发 Cloudflare Origin CA 证书

**文件：**

- 输入：`/home/ubuntu/pixiv.dongboapp.com.csr`
- 创建：`/etc/ssl/certs/pixiv.dongboapp.com.pem`

**接口：**

- 消费：任务 1 生成的 CSR。
- 产出：包含 `serverAuth` 用途并覆盖 `pixiv.dongboapp.com` 的 Cloudflare Origin CA 证书。

- [ ] **步骤 1：撤销已泄露的旧证书**

在 Cloudflare 的 `SSL/TLS → Origin Server` 中撤销与已发送私钥对应的证书。

- [ ] **步骤 2：使用服务器 CSR 创建证书**

在 Cloudflare 创建 Origin CA 证书时选择“使用我自己的私钥和 CSR”，粘贴任务 1 的 CSR；主机名必须包含 `pixiv.dongboapp.com`。

- [ ] **步骤 3：安装并验证证书**

将 Cloudflare 返回的公开证书保存到 `/etc/ssl/certs/pixiv.dongboapp.com.pem`，然后运行：

```bash
sudo chmod 0644 /etc/ssl/certs/pixiv.dongboapp.com.pem
sudo openssl x509 -in /etc/ssl/certs/pixiv.dongboapp.com.pem -noout -subject -issuer -dates -ext subjectAltName -purpose
sudo openssl x509 -noout -modulus -in /etc/ssl/certs/pixiv.dongboapp.com.pem | openssl sha256
sudo openssl rsa -noout -modulus -in /etc/ssl/private/pixiv.dongboapp.com.key | openssl sha256
```

预期：证书覆盖目标域名、可用于 SSL 服务器，并且证书与私钥的摘要一致。

### 任务 3：持久化 Nginx HTTPS 配置

**文件：**

- 修改：`config/nginx/pixiv-novel-sync.conf`
- 修改：`update.sh`
- 创建：`tests/test_deployment_config.py`

**接口：**

- 消费：任务 2 安装的证书和私钥路径。
- 产出：`80` 跳转至 HTTPS、`443` 反向代理至 `127.0.0.1:5011` 的 Nginx 配置。

- [ ] **步骤 1：增加配置结构检查**

创建 `tests/test_deployment_config.py`：

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_nginx_config_enables_strict_https() -> None:
    config = (ROOT / "config/nginx/pixiv-novel-sync.conf").read_text(encoding="utf-8")

    assert config.count("server_name pixiv.dongboapp.com;") == 2
    assert "listen 80;" in config
    assert "return 301 https://pixiv.dongboapp.com$request_uri;" in config
    assert "listen 443 ssl;" in config
    assert "ssl_certificate /etc/ssl/certs/pixiv.dongboapp.com.pem;" in config
    assert "ssl_certificate_key /etc/ssl/private/pixiv.dongboapp.com.key;" in config
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in config
    assert "proxy_pass http://127.0.0.1:5011;" in config


def test_update_script_reports_public_https_url() -> None:
    script = (ROOT / "update.sh").read_text(encoding="utf-8")

    assert 'PUBLIC_URL="https://pixiv.dongboapp.com"' in script
    assert 'echo "访问地址: ${PUBLIC_URL}"' in script
```

- [ ] **步骤 2：运行检查并确认当前配置不满足要求**

运行：

```bash
python -m pytest tests/test_deployment_config.py -q
```

预期：两个测试失败，分别指出 Nginx 尚未监听 `443`，更新脚本尚未声明 `PUBLIC_URL`。

- [ ] **步骤 3：修改 Nginx 和更新脚本**

对 `config/nginx/pixiv-novel-sync.conf` 应用以下精确结构变更；原服务器块中的日志与所有 `location` 保持原位：

```diff
+server {
+    listen 80;
+    server_name pixiv.dongboapp.com;
+    return 301 https://pixiv.dongboapp.com$request_uri;
+}
+
 server {
-    listen 80;
-    server_name _;
+    listen 443 ssl;
+    server_name pixiv.dongboapp.com;
+
+    ssl_certificate /etc/ssl/certs/pixiv.dongboapp.com.pem;
+    ssl_certificate_key /etc/ssl/private/pixiv.dongboapp.com.key;
+    ssl_protocols TLSv1.2 TLSv1.3;
+    ssl_session_cache shared:SSL:10m;
+    ssl_session_timeout 1d;
```

对 `update.sh` 应用以下精确变更：

```diff
 FLASK_PORT=5011
-NGINX_PORT=80
+PUBLIC_URL="https://pixiv.dongboapp.com"
```

```diff
-    echo "访问地址: http://$(hostname -I | awk '{print $1}'):${NGINX_PORT}"
+    echo "访问地址: ${PUBLIC_URL}"
```

- [ ] **步骤 4：运行完整测试与配置静态检查**

运行：

```bash
python -m pytest tests/test_deployment_config.py -q
python -m pytest tests -q
```

预期：部署配置测试 `2 passed`，完整测试退出码为 `0`。

### 任务 4：提交、推送并部署

**文件：**

- 部署来源：`config/nginx/pixiv-novel-sync.conf`
- 部署目标：`/etc/nginx/sites-available/pixiv-novel-sync`

**接口：**

- 消费：任务 3 的仓库配置。
- 产出：服务器上生效的 HTTPS 配置。

- [ ] **步骤 1：提交并推送主分支**

运行：

```bash
git add docs/superpowers/plans/2026-07-20-cloudflare-https.md config/nginx/pixiv-novel-sync.conf update.sh tests/test_deployment_config.py
git commit -m "feat: 配置 Cloudflare 严格 HTTPS"
git push origin main
```

- [ ] **步骤 2：执行现有更新流程**

运行：

```bash
cd ~/pixiv-novel-sync
./update.sh
```

- [ ] **步骤 3：验证源站服务**

运行：

```bash
sudo nginx -t
sudo systemctl is-active nginx pixiv-novel-sync
sudo ss -ltnp | grep -E ':80 |:443 |127.0.0.1:5011'
curl --resolve pixiv.dongboapp.com:443:127.0.0.1 \
  --cacert /etc/ssl/certs/pixiv.dongboapp.com.pem \
  -I https://pixiv.dongboapp.com/nginx-health
```

预期：Nginx 配置有效，两个服务均为 `active`，监听范围符合约束，源站健康检查返回 `200`。

### 任务 5：验证 Cloudflare 端到端 HTTPS

**接口：**

- 消费：任务 4 已生效的源站 HTTPS。
- 产出：Cloudflare `Full (strict)` 下可访问的站点。

- [ ] **步骤 1：设置严格加密模式**

在 Cloudflare 的 `SSL/TLS → 概述` 中将模式设置为 `Full (strict)`。

- [ ] **步骤 2：验证公网 HTTP 跳转和 HTTPS 健康检查**

运行：

```bash
curl -I http://pixiv.dongboapp.com/nginx-health
curl -I https://pixiv.dongboapp.com/nginx-health
```

预期：HTTP 返回到 HTTPS 的重定向，HTTPS 返回 `200`，不再出现 `523`、`525` 或证书错误。

- [ ] **步骤 3：确认应用与日志**

运行：

```bash
curl -I https://pixiv.dongboapp.com/
sudo journalctl -u pixiv-novel-sync --since '-10 minutes' --no-pager
sudo tail -n 50 /var/log/nginx/pixiv-novel-sync.error.log
```

预期：应用页面正常响应，最近日志没有新增 TLS、代理或启动错误。
