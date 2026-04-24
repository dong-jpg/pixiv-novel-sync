# Pixiv Novel Sync

面向 Pixiv 小说的服务器端增量归档工具。目标是只同步小说，不同步插画和漫画；但会保留小说附属封面与正文插图资源。

## MVP 范围

- 使用 `refresh_token` 登录 Pixiv
- 同步当前账号收藏的小说（公开 + 私密）
- 保存小说元数据、正文、封面与正文附图
- 使用 SQLite 维护同步状态
- 提供命令行入口
- 提供 Ubuntu `systemd` 部署模板
- 提供一个受控本地/内网页面用于辅助获取 Pixiv `refresh_token`
- 支持“本地 PC 浏览器授权 + 服务器回调接收 token”主流程
- 保留 `gppt` 作为 fallback

详细设计见 [`pixiv-novel-sync-plan.md`](../.tocodex/plans/pixiv-novel-sync-plan.md:1)。

## 当前实现结构

- [`pyproject.toml`](pyproject.toml:1)：项目依赖与 CLI 入口
- [`src/pixiv_novel_sync/settings.py`](src/pixiv_novel_sync/settings.py:1)：配置加载
- [`src/pixiv_novel_sync/auth.py`](src/pixiv_novel_sync/auth.py:1)：Pixiv 认证管理
- [`src/pixiv_novel_sync/storage_db.py`](src/pixiv_novel_sync/storage_db.py:1)：SQLite schema 与状态存储
- [`src/pixiv_novel_sync/storage_files.py`](src/pixiv_novel_sync/storage_files.py:1)：文件落盘与资源下载
- [`src/pixiv_novel_sync/sync_engine.py`](src/pixiv_novel_sync/sync_engine.py:1)：收藏小说同步逻辑
- [`src/pixiv_novel_sync/jobs/quick_sync.py`](src/pixiv-novel-sync/src/pixiv_novel_sync/jobs/quick_sync.py:1)：MVP 同步任务入口
- [`src/pixiv_novel_sync/oauth_helper.py`](src/pixiv_novel_sync/oauth_helper.py:1)：OAuth 任务生成、PKCE 与 token 交换
- [`src/pixiv_novel_sync/token_helper.py`](src/pixiv_novel_sync/token_helper.py:1)：`gppt` fallback
- [`src/pixiv_novel_sync/webapp.py`](src/pixiv_novel_sync/webapp.py:1)：Web token 获取入口
- [`src/pixiv_novel_sync/templates/token_login.html`](src/pixiv_novel_sync/templates/token_login.html:1)：前台登录页
- [`src/pixiv_novel_sync/templates/oauth_callback.html`](src/pixiv_novel_sync/templates/oauth_callback.html:1)：授权回调页
- [`deploy/systemd/pixiv-novel-sync.service`](deploy/systemd/pixiv-novel-sync.service:1)：systemd 服务模板
- [`deploy/systemd/pixiv-novel-sync.timer`](deploy/systemd/pixiv-novel-sync.timer:1)：定时器模板

## 本地使用

### 1. 安装

```bash
python -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install .
```

### 2. 初始化配置

```bash
cp .env.example .env
cp config/config.yaml.example config/config.yaml
```

至少填写：

- `PIXIV_REFRESH_TOKEN`
- `PIXIV_USER_ID`，如果自动识别失败时必须手动填写

### 3. 验证认证

```bash
pixiv-novel-sync --config config/config.yaml auth-check
```

### 4. 执行收藏同步

```bash
pixiv-novel-sync --config config/config.yaml sync-bookmarks
```

## Web 页面获取 Token

### 启动本地 UI

```bash
pixiv-novel-sync --config config/config.yaml web-token-ui --host 127.0.0.1 --port 5010
```

然后打开：

```text
http://127.0.0.1:5010/token-login
```

## 推荐方式：本地 PC 浏览器授权

页面中点击“开始 Pixiv 浏览器登录”后：
- 后端创建一次性 OAuth 任务
- 页面显示 Pixiv 登录链接
- 你在本地 PC 浏览器中完成登录
- Pixiv 回调到服务器 [`/oauth/callback`](src/pixiv_novel_sync/webapp.py:1)
- 服务器交换 `refresh_token`
- 页面轮询显示结果
- 点击按钮可直接写入服务器 [`.env`](.env.example:1)

### 部署要求

这个方案要求：
- 你的服务器地址或域名能被本地浏览器访问
- 回调地址与页面访问地址一致
- 推荐放在反向代理后并启用 HTTPS

### 安全说明

- 不要把这个页面裸露到完全无保护的公网
- 建议至少加 IP 白名单、基础认证或临时内网访问控制
- `refresh_token` 属于高敏感凭据，不应写入前端持久存储

## fallback：gppt 模式

如果 OAuth 主流程因为 Pixiv 风控或回调链路变化而失败，可以在页面中点击“使用旧版 gppt fallback”。

该模式会：
- 调用 [`gppt login`](src/pixiv_novel_sync/token_helper.py:1)
- 展示输出日志
- 成功后展示 `refresh_token`
- 允许一键写入 `.env`

## 输出说明

默认输出目录：

- 公开内容：[`data/library/public`](data/library/public:1)
- 私密内容：[`data/library/private`](data/library/private:1)
- 数据库：[`data/state/pixiv_sync.db`](data/state/pixiv_sync.db:1)

每本小说目录包含：

- `meta.json`
- `text.txt`
- `text.md`
- `assets/`

## Ubuntu 服务器部署

### 1. 上传代码到服务器

建议目标目录：`/opt/pixiv-novel-sync/app`

### 2. 在服务器执行安装脚本

```bash
bash scripts/install_server.sh
```

### 3. 启动 token 获取页面

```bash
cd /opt/pixiv-novel-sync/app
. .venv/bin/activate
pixiv-novel-sync --config config/config.yaml web-token-ui --host 0.0.0.0 --port 5010
```

### 4. 打开页面

```text
http://你的服务器地址:5010/token-login
```

### 5. 获取并写入 token

优先使用“本地 PC 浏览器授权”主流程。
如果失败，再使用 `gppt` fallback。

### 6. 认证测试与首次同步

```bash
. .venv/bin/activate
pixiv-novel-sync --config config/config.yaml auth-check
pixiv-novel-sync --config config/config.yaml sync-bookmarks
```

### 7. 查看定时器状态

```bash
systemctl status pixiv-novel-sync.timer
systemctl list-timers --all | grep pixiv
journalctl -u pixiv-novel-sync.service -n 100 --no-pager
```

## 已知限制

- 当前只实现“收藏小说”同步闭环，关注用户、关注流、系列追更尚未接入
- 资源下载目前通过通用 HTTP 下载，后续可切换为更贴合 Pixiv 请求头的下载方式
- `webview_novel()` 返回结构在不同版本库中可能略有差异，后续应补充兼容层与测试
- 当前正文更新会直接覆盖文件，尚未保留历史版本快照
- Pixiv 授权链路并非官方开放平台集成方案，未来可能因风控变化需要调整
