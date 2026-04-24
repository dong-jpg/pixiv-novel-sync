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

详细设计见 [`pixiv-novel-sync-plan.md`](../.tocodex/plans/pixiv-novel-sync-plan.md:1)。

## 当前实现结构

- [`pyproject.toml`](pyproject.toml:1)：项目依赖与 CLI 入口
- [`src/pixiv_novel_sync/settings.py`](src/pixiv_novel_sync/settings.py:1)：配置加载
- [`src/pixiv_novel_sync/auth.py`](src/pixiv_novel_sync/auth.py:1)：Pixiv 认证管理
- [`src/pixiv_novel_sync/storage_db.py`](src/pixiv_novel_sync/storage_db.py:1)：SQLite schema 与状态存储
- [`src/pixiv_novel_sync/storage_files.py`](src/pixiv_novel_sync/storage_files.py:1)：文件落盘与资源下载
- [`src/pixiv_novel_sync/sync_engine.py`](src/pixiv_novel_sync/sync_engine.py:1)：收藏小说同步逻辑
- [`src/pixiv_novel_sync/jobs/quick_sync.py`](src/pixiv_novel_sync/jobs/quick_sync.py:1)：MVP 同步任务入口
- [`src/pixiv_novel_sync/token_helper.py`](src/pixiv_novel_sync/token_helper.py:1)：封装 `gppt` token 获取流程
- [`src/pixiv_novel_sync/webapp.py`](src/pixiv_novel_sync/webapp.py:1)：Web token 获取入口
- [`src/pixiv_novel_sync/templates/token_login.html`](src/pixiv_novel_sync/templates/token_login.html:1)：前台登录引导页
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
http://127.0.0.1:5010
```

页面功能：
- 点击“开始获取 Token”后，后端启动 `gppt get`
- 页面轮询显示任务输出
- 成功后展示 `refresh_token`
- 可点击“写入服务器 .env”直接保存

### 安全说明

- 默认只监听 `127.0.0.1`
- 不要直接把这个页面裸露到公网
- 如果要通过外网访问，必须额外加反向代理鉴权或 IP 白名单

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
pixiv-novel-sync --config config/config.yaml web-token-ui --host 127.0.0.1 --port 5010
```

### 4. 填写服务器上的 `.env` 或通过页面一键写入

如果页面已写入，可以直接进行认证测试。

### 5. 试运行一次

```bash
. .venv/bin/activate
pixiv-novel-sync --config config/config.yaml auth-check
pixiv-novel-sync --config config/config.yaml sync-bookmarks
```

### 6. 查看定时器状态

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
- Web token 页面本质上是对 `gppt` 的受控包装，不是官方 OAuth 登录实现
