# NAS / Docker 部署

程序以单个容器运行 Flask、Waitress、SQLite 和现有任务调度器。同步、推荐、EPUB 导出及 AI 调用均在部署主机执行；AI 仍请求你配置的模型 Provider，并不在 NAS 上自动加载大模型。无需 Redis、额外队列或云端 worker。

## 环境与目录

- 使用 Linux 容器和 Docker Compose v2。基础镜像选用支持 `linux/amd64`、`linux/arm64` 的 Python 3.12 Debian；32 位 ARM NAS 不在此镜像支持范围。具体 NAS 的镜像构建、网络和权限需要在设备上验证。
- 只运行一个应用实例。调度器与任务互斥在进程内，不能用多个容器或多个 WSGI worker 共享同一个书库，也不要同时运行旧服务器的自动同步。
- 把项目放在 NAS 自身的本地存储卷上（如 ext4/Btrfs），通过 bind mount 提供给容器。SQLite 使用 WAL，数据库目录不要放在容器通过 SMB/NFS 挂载的网络文件系统上。
- 容器内 `/app/config` 保存 `config.yaml` 和 `.env`，`/app/data` 保存数据库、正文、封面及分析产物。Compose 默认对应项目的 `./config` 和 `./data`，可以在 NAS 容器管理器中改为专用目录。
- 必须挂载整个配置目录并允许写入。设置、Token 和会话密钥通过临时文件及原子替换保存，不能只挂载单个 `.env` / YAML 文件，也不能挂载为只读。

## 首次启动

在包含 `Dockerfile` 和 `compose.yaml` 的项目目录操作。以下复制仅用于新安装，迁移已有书库时按后面的迁移步骤保留原文件：

```sh
mkdir -p config data
cp -n config/config.yaml.example config/config.yaml
cp -n .env.example config/.env
chmod 600 config/.env
```

编辑 `config/.env`：

| 配置 | 用途 |
| --- | --- |
| `DASHBOARD_TOKEN` | 必填强随机访问密码，否则容器拒绝启动，避免出现 NAS 访问全部被拒的情况 |
| `PIXIV_FLASK_SECRET` | 固定会话密钥；新安装留空会自动生成并持久化，迁移时保留原值 |
| `PIXIV_REFRESH_TOKEN` | 同步需要；也可启动后访问 `/token-login` 手动粘贴 |
| `PIXIV_WEB_COOKIE` | 追更系列列表需要，可从已登录的 Pixiv 网页复制 |
| `PIXIV_PROXY` | NAS 不能直连 Pixiv 时填写 NAS 可达的 HTTP/SOCKS 代理地址 |
| `PIXIV_NOVEL_SYNC_AI_SECRET_KEY` | 使用 AI 时需要；迁移已有 Provider 时必须原样保留，否则旧凭据无法解密 |
| `PIXIV_COOKIE_SECURE` | 仅局域网 HTTP 使用 `false`，HTTPS 反向代理使用 `true` |

新密码或密钥可以用 `openssl rand -hex 32` 生成，分别生成，不要把已有加密密钥替换成新值。NAS 的内网 HTTP 直连默认保留 `DASHBOARD_TRUST_PROXY=false`。

容器默认 UID/GID 为 `1000:1000`。NAS 上可用 `id <运行用户>` 查看专用用户 ID，在项目根目录的 Compose `.env`（不同于应用的 `config/.env`）设置：

```dotenv
PIXIV_UID=1000
PIXIV_GID=1000
PIXIV_PORT=5010
```

把 UID/GID 改成实际用户，并通过 NAS 文件管理器或 `chown` 给该用户配置目录、数据目录及其中已有文件的读写权限。不要为了启动把全部私人数据设置成全员可写。镜像不会递归更改现有书库的所有权。

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 pixiv-novel-sync
```

浏览器访问 `http://<NAS局域网IP>:5010/dashboard`，使用刚设置的访问密码登录。首次确认同步设置后手动运行少量任务，再开启自动同步。`config.yaml` 的 `sync.auto_sync_timezone` 决定 cron 时区；如需北京时间请设为 `Asia/Shanghai`，容器的 `TZ` 不会覆盖该配置。

Docker 健康检查访问容器内 `/api/health`，只证明 Web 服务有响应，不证明 Pixiv/模型网络可用。`restart: unless-stopped` 处理进程退出和 Docker 重启；单纯变成 unhealthy 不会自动重启。日志按 10 MiB × 3 轮换。

## 网络、代理与自动登录

容器的 `127.0.0.1` 指向容器自身。NAS 上的代理应填写它实际可达的局域网地址和端口；同一 Docker 网络里的代理可填写服务名。模型 Provider 如使用内网地址，也要调整原来的 localhost URL，并按需设置 `PIXIV_AI_ALLOW_PRIVATE_HOSTS=1`。

默认镜像支持手动粘贴 Token / Cookie、同步、阅读、EPUB 和 AI。只有 Playwright 自动登录、自动刷新 Web Cookie 需要额外 Chromium。需要时在 Compose 根目录 `.env` 增加 `INSTALL_BROWSER=true` 后重新构建：

```sh
docker compose build --pull pixiv-novel-sync
docker compose up -d
```

浏览器及系统依赖在构建时安装，运行时无需下载。此镜像更大，浏览器登录也更占内存；验证码或 Pixiv 登录策略仍可能需要手动操作。

默认端口发布到 NAS 的所有网卡，供局域网访问。仅给 NAS 本机反向代理使用时可设 `PIXIV_BIND_IP=127.0.0.1`；反代若也在容器内，应走共同 Docker 网络或实际可达的 NAS 地址。HTTPS 入口设置 `PIXIV_COOKIE_SECURE=true`；仅在反代会清洗转发头且后端只接受可信反代访问时开启 `DASHBOARD_TRUST_PROXY=true`。Nginx 应为 AI/SSE 禁用响应缓冲并设置足够超时；NAS 反代需按现有 [接口契约](frontend-api-contract.md) 配置请求体大小。

容器不要求 Nginx，图片代理由 Flask 提供。没有 Nginx 图片缓存时，系统页的缓存统计不代表书库附件大小。

## 从原服务器迁移

1. 在旧站关闭自动同步，等正在运行的同步/导出/AI 任务结束或明确取消。停止旧 Web 服务和旧同步 timer，防止两边同时运行。不要把尚未确认结果的 AI 调用当作可以免费重试；原有预算、用量和检查点继续保留。
2. 若以前安装过远程执行，在对应主机停止并禁用 `pixiv-remote-controller`、`pixiv-remote-worker`、`pixiv-remote-egress` 和 `pixiv-remote-gc.timer`。源码更新不会替你关闭已部署服务或取消云平台计费；云机退订/删除另行处理。
3. 停止所有写入后，一致备份原 `.env`、`config/config.yaml` 和完整数据目录。SQLite 的 `-wal`、`-shm` 如存在要与数据库一起保留，不能只复制运行中的 `.db`。AI 分析产物、回收站和书库附件也在数据目录中。
4. 将原 `.env` 放到 NAS 的 `config/.env`，原 YAML 放到 `config/config.yaml`，数据目录按原结构放到 `data/`。保留 `PIXIV_FLASK_SECRET`、AI 密钥及救援相关密钥。旧 `sync.pixiv_execution` 与 `PIXIV_REMOTE_*` / `INSTABOX_API_KEY` 已失效，可以移除；旧值不会恢复云端执行，保存同步设置时会清理 `pixiv_execution`。
5. 默认容器路径为 `/app/data/state/pixiv_sync.db`、`/app/data/library/public`、`/app/data/library/private`，由镜像环境变量覆盖旧 `.env` 的存储路径。旧数据库文件名、目录结构不同时，在 Compose `environment` 中设置这三个 `PIXIV_*_PATH` / `PIXIV_*_DIR` 为对应的容器路径，确认它们都落在持久挂载中。数据库保存的相对 `archive_dir` 可直接使用；历史绝对归档路径或附件路径需先核对，必要时将旧根目录作为额外挂载保留相同容器路径，勿直接批量改写真实库。
6. 启动后检查书库数量、旧正文/封面、EPUB、Provider 凭据和任务设置，再做一次少量本地同步。以前冻结在远程 AI 模式下的未完成分析可能提示配置已变化，需要重新预览确认；不会自动扩大预算或重放旧远程队列。确认后启用 NAS 的自动同步。

旧远程控制库、blob 目录和临时 worker 输出不再参与业务，不需要挂载到 NAS。先保留它们作迁移备份，确认新书库无误后再自行处置。

## 更新与备份

更新前暂停任务并执行 `docker compose stop`，对 `config/` 和 `data/` 做一致备份，再更新代码并执行 `docker compose up -d --build`。重新创建容器会继续使用这两个目录。不要对 Docker 部署执行宿主机的 `deploy.sh` 或 `update.sh`。

恢复时停容器，再一起恢复配置、密钥、数据库和附件。NAS 快照建议在服务停止后创建；恢复旧镜像时也使用与之匹配的数据备份。已有浏览器会话是否继续有效取决于会话密钥、数据库状态和域名。
