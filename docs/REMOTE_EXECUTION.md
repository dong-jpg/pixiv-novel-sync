# 远程执行运维指南

本功能让 VPS 保留唯一书库、阅读入口和任务记录，由 InstaBox 按需执行诊断、单本 EPUB 及固定业务任务。现有本地同步与 EPUB/ZIP 下载保持原契约。云实例暂停、冷却或无法连接时，阅读仍由 VPS 提供。

远程执行保留 `diagnostic.v1`、`epub.single.v1` 手动任务，并增加内部 `pixiv.novel.v1`、`pixiv.api.v1`、`ai.text.v1`，以及显式提交的 `checkin.status.v1`、`checkin.submit.v1`。VPS 仍保存唯一书库、原任务调度、水位和业务预算。[阅读进度同步](READING_PROGRESS_SYNC.md)独立运行。不要向任务协议添加任意 shell、Python、SQL、URL 或文件路径。

## 业务执行位置与可信出口

同步设置的 `pixiv_execution: local|remote` 决定收藏、关注、追更、作者备份、状态检查、待删除检测和推荐中的固定 Pixiv 调用是否走 worker。默认 local。显式 remote 时控制配置/worker 不可用会失败，不静默回退。`archive_rescan_days` 控制旧正文回扫，原分页、请求间隔与任务优先级继续生效。

本版采用**VPS 上游转发**：worker 只请求冻结参数的操作，不取得 Pixiv token/Cookie 或模型 API key；VPS 使用持久发送回执调用上游，云端完成 DTO 整理/校验和结果交付。因此上游公网出口、登录和附件下载仍在 VPS，不能把此模式描述成“所有网络请求已从云机发出”。旧 worker 只声明诊断/EPUB 能力，不会领取新增业务。

可选的 `pixiv-remote egress` 提供云主机 HTTPS CONNECT 出口。VPS 的 `PIXIV_REMOTE_EGRESS_PROXY` 留空时保持上述行为和 Provider 原有代理；填写 `https://worker:<独立URL安全密钥>@<可信云主机>:8443` 后，远程 Pixiv/模型上游连接经该出口发送，失败不会改用本地出口。上游 TLS 仍由 VPS 验证并端到端加密，出口主机不解密 Pixiv token、Cookie、模型 API key 或正文。OAuth 登录与归档附件下载继续使用原 VPS 路径。

出口单独使用 [环境示例](../config/remote-egress.env.example) 和 [systemd unit](../deploy/systemd/pixiv-remote-egress.service)。部署时提供受 VPS 信任的证书、仅服务用户可读的私钥、至少 32 字符的独立随机密钥、实际 Pixiv/Provider 主机名白名单，并限制入口仅允许 VPS。默认只监听回环；跨主机部署必须明确设置监听地址和防火墙。代理仅允许白名单解析出的公网地址与 443 端口，不接受私网/元数据地址、其他端口或任意代理目标。单机最多四个隧道，每个最多十分钟/128 MiB；证书过期或域名白名单缺失会导致远程任务失败，应先验证再启用。生产出口尚未部署验证。

Web 配置增加 `PIXIV_REMOTE_TASK_SECRET`（至少 32 字符的稳定随机值，用于内部临时 Pixiv 凭据加密，可使用已配置的 AI secret 作为回落）。`PIXIV_REMOTE_AI_EXECUTION=remote` 可让统一文本调用走云任务协议；默认 local。模型发送前仍执行原父子预算事务，不更换已有 Provider/路由组，也不自动重播未知结果。输入用每任务临时密钥加密，密钥仅在 VPS 当前进程内存；重启后遗留密文不可重放。

升级顺序：先一致备份业务库与控制库，再停派发/worker，升级 Web/controller 和 worker，核对控制库 schema v2 与能力声明，先诊断后测试单篇/各类业务。新版本 schema 不能由旧 controller 直接读取；回退要按本文恢复流程处理，不能仅回退 Python 代码。`sync-bookmarks` CLI 的 Pixiv 远程任务需同一在线 Web worker API；独立 CLI 的远程 AI 不支持共享进程内授权，应通过 Web 业务入口执行。

签到是可选功能，worker 须安装兼容 `daily-checkin` 并配置独占的已授权浏览器状态；见 [签到适配](CHECKIN_STATUS_ADAPTER.md)。不自动搬迁个人浏览器配置或登录会话。

## 状态与验证边界

页面入口为 `/dashboard/remote`。当前代码验证和生产边界统一见 [项目现状](STATUS.md)，租约/发布/数据源约束见 [远程执行设计](REMOTE_EXECUTION_DESIGN.md)，完整业务接线顺序见 [实施计划](IMPLEMENTATION_PLAN.md)。

代码默认关闭。仓库中的服务、环境文件和 Nginx 配置都是待安装示例，运行本地测试不会启动用户实例，也不会安装或启用服务。

自动测试使用临时控制数据库、合成小说、fake provider 和本地 HTTP transport。[部署验证脚本](../scripts/verify_remote_deployment.py)用于 Linux unit、TLS、代理、限流和上传并发检查；[页面验证脚本](../scripts/verify_remote_ui.py)使用离线 Vue 和真实 Flask fixture。完整 VM 冷启动、平台强制中断/冷却、私有输入与独立备份须在目标环境另验，不能由本地测试推断。

## 进程、目录与凭据

| 进程 | 所在主机 | 配置文件示例 | 可读凭据及数据 |
|---|---|---|---|
| 现有 Web 服务 | VPS | `config/remote-web.env.example` | 原书库权限、worker bootstrap 的 SHA-256；不需要 InstaBox key |
| `pixiv-remote controller` | VPS | `config/remote-controller.env.example` | 指定机器的 InstaBox key、独立控制库；没有 worker bootstrap 明文 |
| `pixiv-remote worker` | InstaBox | `config/remote-worker.env.example` | 独立 worker bootstrap、任务级输入；可选签到独占会话另行配置，没有书库 DB、Pixiv 凭据、Flask secret 或 AI 解密密钥 |
| `pixiv-remote gc` | VPS | `config/remote-maintenance.env.example` | 控制目录与机器 ID；不需要平台、worker 或业务密钥，不联网 |
| `pixiv-remote egress`（可选） | 可信云主机 | `config/remote-egress.env.example` | 出口独立密钥、TLS 证书/私钥及主机白名单；没有上游账户凭据 |

控制目录建议 `/var/lib/pixiv-remote`，其中 `control.db` 为独立 SQLite，文件存储位于 `blobs/`。不要把它配置成业务主库或归档目录。Web 与 controller 需要对该目录具备必要的共享读写权限，建议使用相同专用服务用户；不要授予 Nginx 读取配置秘密的权限。

worker 的专用工作目录建议 `/var/lib/pixiv-remote-worker`，仅由其服务用户拥有。首次启动要求空目录，worker 创建自己的标记及 UUID 任务目录。它只清理经过路径校验的自有文件；不递归删除操作员目录。运行期间不得在该目录放其他文件或创建符号链接。

环境配置只从显式进程环境读取。独立 CLI 不搜索 `.env` 或 `config.yaml`，不调用业务 `load_settings()`。systemd 示例使用两个不同的 `EnvironmentFile`；不要把 VPS 整份 `.env` 复制到云端。

Web/controller 的 `PIXIV_REMOTE_ENABLED` 只接受 `0`、`1`；未设置等同 `0`。worker 不依赖这个开关，由其独立服务是否启动决定；缺失 origin、专用目录或独立 key 时拒绝启动。关闭示例配置不会自动安装 worker 服务。

## 独立凭据生成

bootstrap 使用至少 256-bit 的随机值。以下示例在受保护的本地目录生成密钥和摘要文件，终端不打印明文；只在已批准的安装阶段执行，并检查文件所有者。不要将生成文件加入 Git。

```python
from pathlib import Path
import hashlib
import os
import secrets

directory = Path("/etc/pixiv-remote/bootstrap-install")
directory.mkdir(mode=0o700, parents=True, exist_ok=False)
key = secrets.token_urlsafe(32)
for name, value in (("worker-key", key),
                    ("worker-key.sha256", hashlib.sha256(key.encode("ascii")).hexdigest())):
    descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as target:
        target.write(value + "\n")
```

将 key 安全安装到 worker 配置的 `PIXIV_REMOTE_WORKER_KEY`，仅把摘要安装到 Web 配置的 `PIXIV_REMOTE_WORKER_KEY_SHA256`；两个配置文件权限为 `0600`。每次进程冷启动还会生成新的 boot UUID 和 session secret，服务端只存 session secret 的摘要，旧 boot 不能重新取得权限。

InstaBox key 单独安装到 VPS controller 配置，绑定明确核对过的 `PIXIV_REMOTE_MACHINE_ID`。本实现不遍历账户机器，不调用 delete，也不通过 exec 启动业务。

## 离线检查与运行入口

项目安装后会生成独立命令 `pixiv-remote`，也可用 `python -m pixiv_novel_sync.remote.cli`。不要把 worker 接到原有主 CLI、业务 timer 或 Web 初始化线程中。

```console
pixiv-remote doctor
pixiv-remote doctor --role controller
pixiv-remote doctor --role worker
```

`doctor` 默认检查 Web 角色，只检查显式配置、路径与权限，不联网、不创建数据库或启动服务。输出 `network: not_checked` 就表示没有验证连通性。`controller --once` 或 `worker --once` 只执行一个监督周期；它们在启用并配置后可能产生真实网络请求，不能当作离线 doctor 的替代。

远程页出现 `provider_unreachable` 表示 VPS 控制器未能完成对 InstaBox 管理接口的请求。浏览器刷新只读取控制库，“实例状态”是上次成功确认的值。暂时断线按退避间隔重试；下一次成功观察到实例且没有在途启停操作时，清除旧连接错误和连续失败次数。认证失败和结果未确认的启停操作保留各自的处理规则。

如错误持续，可在 VPS 上用控制器相同的服务用户、环境和网络运行 `pixiv-remote doctor --role controller --network`。它只向配置的实例发一次状态 GET，不启动或暂停实例，不创建数据库。按仓库中的 systemd 安装路径，示例为：

```console
sudo systemd-run --wait --pipe --collect -p User=pixivsync -p EnvironmentFile=/etc/pixiv-remote/controller.env /opt/pixiv-novel-sync/app/.venv/bin/pixiv-remote doctor --role controller --network
```

`network=reachable` 表示本次请求通过；`reason=connection` 检查 VPS 的 DNS、路由及直连 443，`timeout` 表示连接或读取超时，`tls` 检查时间和证书链；HTTP 401/403 检查控制器的平台密钥与权限。控制器直连 `api.instabox.sh`，不读取环境代理，模型 Provider 代理或 `PIXIV_REMOTE_EGRESS_PROXY` 不用于这个接口。不要把密钥放入命令行或日志。若使用了额外的 systemd 网络隔离，测试时也要复用相同设置。

controller 与 worker 主入口分别是：

```console
pixiv-remote controller
pixiv-remote worker
```

正常生产入口保持循环，不使用 `--once`。worker 监督循环独立于传输线程；渲染在固定 spawn 子进程运行，子进程在调用渲染器前清除继承的敏感环境变量。SIGTERM 只停止自己的子进程与网络处理，不会把云实例直接暂停。

一期渲染依赖使用[版本约束](../config/remote-renderer-constraints.txt)，与已验证的 VPS 对齐为 EbookLib 0.20、lxml 6.1.1。worker 的专用虚拟环境可安装最小依赖：

```console
python -m pip install -c config/remote-renderer-constraints.txt "requests>=2.32.3" "urllib3>=2,<3" EbookLib lxml
```

两端升级渲染依赖时，应同步更新约束并重新比较合成 EPUB 的正文、封面和格式。EbookLib 0.21 改变了默认封面样式，本次联调据此补齐版本约束。

## 服务与反向代理安装准备

先核对部署目录、虚拟环境、用户及上游端口，再安装：

- `deploy/systemd/pixiv-remote-controller.service`：VPS，示例用户 `pixivsync`。
- `deploy/systemd/pixiv-remote-worker.service`：云实例，示例用户 `pixivremote`，`Restart=always`、`KillMode=control-group`。
- `deploy/systemd/pixiv-remote-gc.service` / `.timer`：VPS 独立维护，按下节显式安装启用。
- `config/nginx/remote-worker-location.conf`：加入现有 HTTPS server，不覆盖整个站点配置。

服务示例不会自动修改既有 `pixiv-novel-sync.service` 或其 timer。目标实例必须确认在冷启动后启动 worker，不能依赖图形桌面登录或只验证内存恢复。

从 wheel 安装时，上述环境、systemd 与 Nginx 示例同时位于安装前缀的 `share/pixiv-novel-sync/`，仍须操作员按目标路径安装；安装 Python 包不会自动启用系统服务。开发者可用 `python scripts/verify_wheel.py <wheel>` 检查隔离安装的页面/资源，写作分支增加 `--writing`。

Nginx 示例顶部的 `limit_req_zone` 必须放在 `http {}` 内一次，不能放进 `server {}`；其余 location 放入 HTTPS server。bootstrap `/sessions` 入口按每 IP 每分钟 6 次、burst 3 限制，返回 429 与 `Retry-After`。共享出口会共享该 IP 桶。认证后的 worker RPC 另受服务端 session 速率限制，认证仍是必要条件。

上传 location 上限 33 MiB，应用继续按 endpoint 严格执行普通 JSON 16 KiB、清单 64 KiB、结果 32 MiB 等限制。不要用 Nginx 总上限替代应用校验。为 Web 配置至少两个可同时处理请求的 Waitress 线程，确保一次上传不会阻塞心跳。

bootstrap 与普通 worker location 都显式设置 `gzip off` 和上游 `Accept-Encoding identity`，使响应符合 worker 的 identity 编码要求。上游使用 `proxy_http_version 1.1` 并清空 `Connection` 头；关闭 `proxy_request_buffering`，让应用能够在接收过程中执行身份、配额及字节限制校验，避免 Nginx 先缓存完整上传。`proxy_buffering off` 与 `proxy_cache off` 关闭响应缓冲和缓存；服务端仍返回 `private, no-store`。将这两处 location 一并安装，避免精确匹配的 `/sessions` 入口遗漏相同的传输设置。

配置完成后在 Linux 上对安装的全部 service / timer 执行 `systemd-analyze verify`，并用包含该 zone、location、证书和实际 upstream 的完整测试配置执行 `nginx -t`。本地配置检查不自动启用线上服务。

## 队列与生命周期语义

单台机器只有一个执行槽。最多 20 个未终结 run，每次 attempt 上限 10 分钟，手动任务提交一小时后过期。纯计算任务最多执行三次；输入、协议或格式错误不会无限重试。

VPS 每个任务保存冻结输入清单，用户同一个 `Idempotency-Key` 重试得到同一 run，即使小说已经更新。worker 领取、心跳和发布都绑定 session、epoch、fence、attempt 与 input digest。上传成功只代表暂存；只有持久化发布回执才代表任务成功。

服务端租约 180 秒，worker 每 30 秒续租，距最后一个有效确认的首次发送时间达到 120 秒即停止工作。延迟响应、重传同一 seq 或机器恢复不会凭空增加这段预算；十分钟硬截止也不会被心跳延长。断线、长暂停或冷启动必须重新握手，不能从磁盘恢复旧执行授权。

controller 将启动/暂停意图和幂等键写入控制库，收到 HTTP 202 后继续核对 operation 与 runtime；还要有新鲜的 worker 会话、足够运行预算及可用存储才允许领取。`running` 不是任务成功，runtime ready 也不是结果已发布。

只有真实任务需求才会唤醒暂停机器。运行期间的有效任务或人工 hold 可以触发有界 wake；hold 本身不会唤醒停机实例。空闲五分钟后，controller 先关闭派发、核查新任务/hold/上传，再暂停自己能证明激活归属的运行会话。人工打开的机器或无法证明归属的会话不会自动暂停。

暂停请求已经发出时新增任务或 hold，必须先等该 operation 收敛，再决定恢复。平台不提供由本地 fencing 撤回已发送 suspend 的保证。界面因此可能显示“暂停请求已发，恢复等待中”。

InstaBox 最大连续运行时间、冷却及账号级限流以实际响应为准；wake 不能延长最大运行时间。不安排固定晚间保活，也不通过轮换机器规避冷却。

## 故障、保留与维护

| 现象 | 处理 |
|---|---|
| 领取或心跳响应丢失 | 沿用原请求键/seq 与首次发送时刻，不再领取另一任务 |
| 发布响应丢失 | 查询原 run 的结果回执，不先重算；仅已确认成功才清理产物目录 |
| lease 过期或冷启动 | 停止旧 child，重新握手；VPS 决定是否生成新 attempt |
| provider 401/403 或异步 operation 认证失败 | 持久记为 `provider_authentication`，修复配置后由操作员恢复；重启不会重新提交 |
| 长期未知 operation/过期 action | 保留操作记录，停止 mutation，人工核对真实平台状态；不把未知 suspend 当作成功 |
| `activation_unconfirmed` / `suspend_uncertain` | 启停回执或运行归属不确定，且旧任务可能已经过期；核对平台状态后离线恢复，避免重新启动一个已经没有需求的实例或误停人工会话 |
| `provider_rejected` | 包括异步 operation 的额度/余额/配置拒绝；核对限制与操作状态后恢复，控制器不持续重发 wake/start。暂时的容量失败使用有上限退避 |
| `upload_in_progress` / 上传响应丢失 | worker 在有效授权下最多三次重传同一 attempt、摘要和大小；其他冲突立即拒绝，持续忙交给 VPS 的有界重试 |
| 磁盘满或配额不足 | 固定错误码，任务不成功；检查专用目录容量后使用安全 GC |

VPS 文件配额 1 GiB，包含进行中的预留空间；worker 工作目录上限 128 MiB。worker 收到服务端失败/终态确认或另一 attempt 已发布的确认后会释放旧任务缓存；无法确认的发布保留回执并周期核对，超过完整任务的一小时时限后可清理本地可丢弃缓存，不据此宣布成功。VPS 终态输入和产物保留 24 小时，元数据保留 14 天，过期下载返回 410。普通 GC 不删除活动任务引用，也不删除当前 epoch 的退役 boot 记录。当前 epoch 达到 boot 容量上限时，需要离线维护，而不是删除 tombstone 使旧凭据复活。

GC 使用“事务标记 deleting → 删除已校验路径文件 → 数据库确认”的可重入流程，新引用不能绑定 deleting 对象。未知临时/孤儿文件至少保留一小时，只在受控 blob 目录内处理。

仓库提供独立 `pixiv-remote-gc.timer`，**显式安装启用后**每小时清理一次，附加最多五分钟随机延迟；停机错过的轮次会在恢复后补一次。controller 不执行 GC；未安装 timer 的环境仍需主动运行。24 小时 / 14 天是保留阈值，不是精确到期删除时间。

以下命令要求路径明确且两次一致；命令不会搜索或下载数据库：

```console
pixiv-remote gc --state-dir /var/lib/pixiv-remote --confirm-state-dir /var/lib/pixiv-remote
```

运行环境还需 `PIXIV_REMOTE_MACHINE_ID` 与控制库一致；配置目录若设为其他值，CLI 拒绝不一致目标。目录/数据库不存在时失败，不自动寻找或初始化业务库。

### 安装定时清理

以下操作在已确认的 VPS 维护窗口执行；它只维护本地控制目录，不唤醒云机。先把 `config/remote-maintenance.env.example` 安装为 `/etc/pixiv-remote/maintenance.env`，填写已有机器 ID，确认目录并设为仅维护用户可读。不要复制 controller 的平台 key。

核对 service 的程序路径、`User=pixivsync` 与现有控制库权限。自定义控制目录时同时修改环境和 `ReadWritePaths`；控制目录必须包含 SQLite WAL/SHM 与 blobs，不能设为业务书库目录。然后执行：

```console
sudo install -m 0644 deploy/systemd/pixiv-remote-gc.service deploy/systemd/pixiv-remote-gc.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/pixiv-remote-gc.service /etc/systemd/system/pixiv-remote-gc.timer
sudo systemctl daemon-reload
sudo systemctl start pixiv-remote-gc.service
sudo journalctl -u pixiv-remote-gc.service --no-pager -n 30
sudo systemctl enable --now pixiv-remote-gc.timer
sudo systemctl list-timers pixiv-remote-gc.timer
```

oneshot 成功后显示 inactive 是正常结果；核对退出状态与输出的删除计数。路径、机器 ID 或数据库校验失败会非零退出，先修配置再重跑。服务使用 `PrivateNetwork=true`、只读系统保护、低 I/O 优先级和五分钟启动时限；超时后下轮沿用 deleting 标记继续，不把部分清理当完整成功。

停用定时触发使用 `sudo systemctl disable --now pixiv-remote-gc.timer`；如需同时中断已启动的清理，另执行 `sudo systemctl stop pixiv-remote-gc.service`。普通 Web 部署/更新不会自动安装或启用该 timer。长期未启用或持续清理失败仍可能用尽配额，需要监测 unit 状态和可用空间。

## 备份恢复与回退

生产上线前先确认业务主库和归档、控制库/产物、配置以及解密所需密钥都有可恢复的一致备份。对活跃 SQLite 使用一致快照或受控停写备份，禁止跨主机同步正在写入的 DB/WAL/SHM。备份加密并异地保存，云端可丢弃缓存不算备份。

从旧控制库备份恢复前，停止所有远程 worker API 进程与 controller；保留已知 provider operation 记录并核对机器实际状态。把显式环境的 `PIXIV_REMOTE_ENABLED` 设为 `0`，等待或确认 controller leader 租约失效后执行：

```console
pixiv-remote recover --rotate-epoch --api-offline --state-dir /var/lib/pixiv-remote --confirm-state-dir /var/lib/pixiv-remote
```

`--api-offline` 是操作员对所有 Web 进程已经关闭的明确声明；CLI 还会拒绝仍启用的环境和活跃 controller leader。恢复会更换 epoch、撤销旧 session/attempt、关闭 gate、放弃无法证明的实例 ownership；已发送的 provider operation 继续保留供核对。不要通过手动改 SQL 删除未知操作来绕过这一检查。

恢复后重新生成 bootstrap key/hash，安全替换两端配置并完成离线检查。明确重新启用配置并重启 controller/API 后，新 controller 只解除恢复留下的 disabled 状态，派发 gate 仍保持关闭，实例 ownership 仍未知；必须先核对保留的在途 operation，再按正常就绪规则派发合成测试。普通进程重启不会自动旋转 epoch；不应把每次重启当成备份回退。

一般回退顺序：停止新提交和 claim，撤销活动 attempt，核查未确认结果，再停止 controller 与 worker；最后关闭 Web/controller 开关。保留控制 DB、输入与结果供检查，不删除业务主库和归档。原有本地同步/导出仍可用，不需要合并云端数据库或临时打开另一套 cron。

## 分步启用与验收记录

1. 保持默认关闭，完成 fake transport、临时数据库、合成 EPUB 的自动验证及页面检查。
2. 获得目标机器和时间窗口授权后，确认生产版本、资源、备份、专用用户、配置权限和两端 HTTPS/TLS。
3. 安装并检查独立服务与代理配置；确认冷启动自动启动 worker。
4. 只提交合成诊断，分别记录 202、operation、runtime/worker readiness、回执和空闲暂停。
5. 提交合成 EPUB，验证内容语义、传输中断、回执丢失及恢复；核查 VPS 阅读和原下载接口仍正常。
6. 由用户明确选择一本真实小说进行小规模试用，再决定是否扩大范围。

每项记录测试时间（北京时间）、目标实例、版本、结果与未验证条件。不能用一次本地 GREEN 或一次 API 200 代替所有真实账户、恢复和资源限制验证。
