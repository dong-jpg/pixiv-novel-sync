# InstaBox Phase One Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不搬迁业务数据库、不影响现有阅读/同步的前提下，交付 VPS 调度、InstaBox 按需运行、单本 EPUB 回传及安全暂停的可恢复试点。

**Architecture:** VPS 增加独立的控制 SQLite 和输入/产物目录，现有 Flask 注册用户 API 与严格隔离的 worker API，但不启动生命周期线程。独立 controller 调用 InstaBox，独立 worker 主动连接 VPS；一期只接受无外部副作用的诊断和 EPUB 任务。

**Tech Stack:** Python 3.10+, Flask 3.0.3+, requests, sqlite3, hashlib, multiprocessing, ebooklib, pytest, existing Jinja/Library OS styles, systemd. No new mandatory service or package.

**Approved Spec:** `docs/superpowers/specs/2026-11-10-vps-instabox-execution-design.md`；用户于 2026-11-10 回复“没问题，继续”。

**Status:** 实施计划，不是实现报告。功能、测试及部署步骤尚未执行；编写基线 `91c54aa`，执行基线必须包含本计划提交。

## Global Constraints

- VPS 是长期在线的业务主服务和数据权威；InstaBox 是可随时中断、可重建的按需执行节点。
- 阅读时段不再是云实例的保活时段。不建立晚间七小时的固定保活循环。
- 第一期仅允许诊断和单本 EPUB 导出，不迁移收藏同步、签到或定时备份，不建设通用远程命令平台。
- 正文上限 16 MiB、封面上限 5 MiB、输出上限 32 MiB，均为字节限制；超限拒绝，不截断。
- 每次执行时限 10 分钟，手动任务截止为提交后 1 小时；首次加最多两次重试，共最多 3 次。
- 一台实例一个执行槽；心跳 30 秒、租约 180 秒、worker 保守停止期限 120 秒；空闲宽限 5 分钟。
- 保留现有同步导出返回 EPUB/ZIP 的契约，不让普通下载等待云端冷启动，不占现有同步执行槽。
- InstaBox 管理 key 只在 VPS controller；worker 无书库 DB、Pixiv/签到凭据、AI 解密密钥、Flask secret。
- 不同步活跃 SQLite/WAL，不挂载远程 SQLite，不提供 SQL、命令、任意 URL/文件路径执行接口。
- 当前是一套私有 Dashboard，不假设已有多用户模型。用户 API 沿用有效 cookie session 和 CSRF；worker token 不授权用户 API。
- 默认测试用临时目录、合成文本及 fake transport，不读真实 `.env`/`config.yaml`，不访问真实 InstaBox/Pixiv。
- 本计划不授权启动用户实例、安装线上服务、推送、合并或部署。真实联调另行确认目标及时间窗口。

## Execution Workspace

实施前读取 using-git-worktrees skill，检查本聊天已有 worktree，优先复用合适的隔离 checkout。需要创建时使用 app 原生工具，`ref` 指向包含本计划的本地提交，不使用远端旧 main。新分支使用 `codex/` 前缀。

原 checkout `D:\gitcode\pixiv-novel-sync` 中 README、RESCUE_USER_GUIDE、STATUS、UNIFIED_PROJECT_REQUIREMENTS、frontend-api-contract、frontend-pages 的修改，以及 `.probe012/`、`docs/codex-for-oss-application.md` 均为既存用户工作。不要覆盖、夹带提交。以下路径按实施 worktree 根解析。

新建专用进度记录 `docs/superpowers/execution/2026-11-10-instabox-phase-one.md`，每项记录 RED、GREEN、审查、commit 和待外部验证项，不覆盖其他工作账本。

```powershell
git status --short
git rev-parse HEAD
python --version
python -m pytest -q
python -m compileall -q src
python scripts/build_tailwind_css.py --check
```

先检查 interpreter/依赖，不重绑共享环境的 editable install。基线失败先记录并报告，不把历史通过数算作本次证据。

## File Map And Order

| Task | 独立交付 | 主要所有权 | 依赖 |
|---|---|---|---|
| 1 | 封闭协议、关闭默认值及测试隔离 | `remote/config.py`, `remote/contracts.py`, `tests/remote/conftest.py` | 无 |
| 2 | 持久队列、会话和租约 | `remote/schema.py`, `remote/store.py`, `remote/sessions.py` | 1 |
| 3 | 输入快照、文件校验及原子发布 | `remote/blobs.py`, `remote/epub_task.py`, `remote/results.py` | 1、2 |
| 4 | 用户/worker 认证与 HTTP 接口 | `remote/web.py`, `remote/worker_api.py`, `webapp.py` | 2、3 |
| 5 | InstaBox 精确协议适配器 | `remote/instabox.py`, provider fixtures | 1，可与 2/3 分开实施 |
| 6 | 需求驱动生命周期与安全暂停 | `remote/lifecycle.py`, `remote/control_store.py` | 2、5 |
| 7 | 专用 worker、心跳与冷启动恢复 | `remote/worker.py`, `remote/worker_http.py`, `remote/runner.py` | 3、4、6 |
| 8 | 可见、可控的试点页面 | `templates/dashboard_remote.html`, `templates/base.html`, `remote/web.py` | 4、6 |
| 9 | 独立进程、部署工件及恢复命令 | `remote/cli.py`, `pyproject.toml`, 新 systemd/config 示例 | 6、7 |
| 10 | 故障闭环与兼容性验收 | `tests/remote/test_end_to_end.py`, `docs/REMOTE_EXECUTION.md` | 1–9 |

源码模块根为 `src/pixiv_novel_sync/remote/`，测试根为 `tests/remote/`。新增空 `remote/__init__.py`。不要修改 `JobType`、将远程等待塞进 `JobRunner`，或向主库 `task_logs` 写 running 远程记录；一期独立队列和状态页避开旧启动清理。

## Fixed Configuration And Protocol

只读取显式环境映射，不调用现有 `load_settings()` 或自动搜索 dotenv。默认关闭时不建控制目录、不注册 worker 路由、不发网络请求。

| 配置 | 进程 | 规则 |
|---|---|---|
| `PIXIV_REMOTE_ENABLED` | Web/controller | 仅 `1` 开启；`0`/未设置关闭；其他值报错 |
| `PIXIV_REMOTE_STATE_DIR` | Web/controller | 开启时绝对专用目录；DB 为其下 `control.db`，拒绝与主库同路径 |
| `PIXIV_REMOTE_MACHINE_ID` | Web/controller | 必填唯一目标；请求不能另选机器 |
| `PIXIV_REMOTE_WORKER_KEY_SHA256` | Web | bootstrap key 的 64 位 hex 摘要 |
| `INSTABOX_API_KEY` | controller | 生命周期凭据，不需要 delete 权限 |
| `PIXIV_REMOTE_SERVER_ORIGIN` | worker | HTTPS origin，无 userinfo/query/fragment/非根 path |
| `PIXIV_REMOTE_WORKER_KEY` | worker | 独立 256-bit 随机凭据，至少 32 个 ASCII 字符，仅用于注册 |
| `PIXIV_REMOTE_WORK_DIR` | worker | 绝对专用目录，不复用书库目录 |

Web 无 provider key；controller 无 bootstrap 明文；worker 无业务配置。密钥字段 `repr=False`。日志仅固定错误码，不打印正文、认证头或上游响应全文。

容量：最多 20 个未终结 run、一个 active attempt；VPS 管理文件总量上限 1 GiB，活动上传的预留量计入；worker 工作目录上限 128 MiB。终态输入/结果保留 24 小时，元数据保留 14 天；过期下载 410。GC 不删除活动租约/未终结任务引用的文件。

任务只允许：

- `diagnostic.v1`：服务器生成 32-byte hex challenge；固定 JSON 输出 `{challenge,protocol:1,renderer:"epub-v1"}`，不采集环境或运行命令。
- `epub.single.v1`：小说 ID、标题、作者、正文/封面摘要和长度、协议/渲染器版本。正文、封面分开传输；不得直接发送整个 `get_novel_detail()` 字典。
- 元数据 JSON 上限 64 KiB；普通 RPC JSON 上限 16 KiB；结果一期一份，`chunk_id=0`，最大 32 MiB，失败可整文件重传，不建通用分块平台；诊断结果另限 4 KiB。
- `request_sha256` 是规范化用户请求；`input_sha256` 是冻结输入清单。相同幂等键先查原请求，不重新读取已变化的小说。导出校验冻结快照，不要求当前小说版本仍相同。

### User API

均使用原 Dashboard 认证；变更校验 CSRF。固定 scope=`dashboard`，不把浏览器 session ID 当持久 owner，换设备登录可查看同一私有书库任务。

| 方法/路径 | 契约 |
|---|---|
| `POST /api/dashboard/remote/runs` | 必填 `Idempotency-Key`；body 只有 kind 和 EPUB 的 novel_id；诊断不收任意 payload；202 `{run_id,state}` |
| `GET /api/dashboard/remote/runs` | 最近最多 50 条脱敏记录 |
| `GET /api/dashboard/remote/runs/<run_id>` | 状态、时间、尝试次数、错误码、下载标志 |
| `POST /api/dashboard/remote/runs/<run_id>/cancel` | 撤销 active attempt；已成功结果不回滚 |
| `GET /api/dashboard/remote/runs/<run_id>/download` | 仅已成功且未过期；附件下载，不收路径参数 |
| `GET /api/dashboard/remote/machine` | 缓存状态、最后确认、冷却/hold；不在 HTTP 线程调用 provider |
| `POST /api/dashboard/remote/machine/hold` | `{seconds:0..14400}`；禁止自动暂停，不单独唤醒已停机器 |
| `GET /dashboard/remote` | 试点页面；未开启显示关闭，不启动服务 |

### Worker API

Blueprint 名 `remote_worker`，前缀 `/api/remote-worker/v1`。全部响应 private/no-store；不启用 CORS；拒绝 Cookie、Origin 和 query token。Bootstrap 只注册 session；session token 不授权任何用户 API。

| 方法/相对路径 | 契约 |
|---|---|
| `POST /sessions` | bootstrap Bearer；`{boot_id,session_secret,protocol,renderer}`；client 生成随机 secret，server 仅存 hash；返回 session_id/epoch/expires_at |
| `POST /session-heartbeat` | session Bearer；`{session_id}`；只续会话，不续任务租约 |
| `POST /claim` | session Bearer；`{claim_key}`；无任务 204，否则 Grant；同 key 返回原领取 |
| `GET /attempts/<attempt_id>/input/<part>` | session+grant；part 仅 manifest/text/cover；无封面 404 |
| `POST /attempts/<attempt_id>/heartbeat` | `{epoch,fence,input_sha256,seq}`；同 seq 返回原确认不续期，过期不复活 |
| `PUT /attempts/<attempt_id>/result` | raw bytes；Content-Length、X-Content-SHA256 和 grant 头；接收后再次核验租约 |
| `POST /attempts/<attempt_id>/publish` | grant 字段及 sha256/size_bytes；原子发布，可幂等读取已有成功确认 |
| `POST /attempts/<attempt_id>/fail` | grant + 白名单 error_code；server 决定重试，不接受 retryable 布尔值 |
| `GET /runs/<run_id>/receipt` | 当前 session 仅查目标机器的结果元信息，不返回正文/路径/下载链接 |

grant 头为 `X-Remote-Epoch`、`X-Remote-Fence`、`X-Remote-Input-SHA256`。token 只在 Authorization。UUID、摘要、整数类型和请求字段集合严格校验；未知字段拒绝，不忽略任意命令/路径。

## Task 1: Contracts, Config And Test Isolation

**Files:** Create `remote/{__init__,contracts,config}.py`, `tests/remote/{conftest,test_contracts,test_config}.py`。

**Interfaces:** `RemoteConfig.from_env(env: Mapping[str,str], role: str) -> RemoteConfig`; `canonical_bytes(value: object) -> bytes`; `digest(data: bytes) -> str`; `request_digest(kind: str, novel_id: int | None) -> str`; immutable `Grant`; `RemoteError(code: str, status: int=409, *, run_id: str | None=None, attempt_id: str | None=None)`。

- [ ] **Step 1: Write failing tests.**

```python
import pytest
from pixiv_novel_sync.remote.contracts import RemoteError, request_digest
from pixiv_novel_sync.remote.config import RemoteConfig


def test_disabled_needs_no_secrets_or_disk(tmp_path):
    assert RemoteConfig.from_env({}, role="web").enabled is False
    assert list(tmp_path.iterdir()) == []


def test_registry_is_closed():
    for kind in ("shell", "sync bookmark", "checkin", "epub.single.v2"):
        with pytest.raises(RemoteError, match="unsupported_kind"):
            request_digest(kind, None)
    with pytest.raises(RemoteError, match="invalid_novel_id"):
        request_digest("epub.single.v1", True)
    assert request_digest("epub.single.v1", 42) != request_digest("epub.single.v1", 43)
```

- [ ] **Step 2: RED.** `python -m pytest tests/remote/test_contracts.py tests/remote/test_config.py -q`；预期缺模块/实现，不是网络或真实配置失败。
- [ ] **Step 3: Add the contract core and role validation.**

```python
from dataclasses import dataclass
import hashlib
import json

PROTOCOL = 1
RENDERER = "epub-v1"
KINDS = frozenset({"diagnostic.v1", "epub.single.v1"})
TEXT_LIMIT = 16 * 1024 * 1024
COVER_LIMIT = 5 * 1024 * 1024
RESULT_LIMIT = 32 * 1024 * 1024
LEASE_SECONDS = 180
HEARTBEAT_SECONDS = 30
STOP_SECONDS = 120
ATTEMPT_SECONDS = 600
RUN_SECONDS = 3600
MAX_ATTEMPTS = 3


class RemoteError(RuntimeError):
    def __init__(self, code: str, status: int = 409, *,
                 run_id: str | None = None, attempt_id: str | None = None):
        self.code, self.status = code, status
        self.run_id, self.attempt_id = run_id, attempt_id
        super().__init__(code)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_digest(kind: str, novel_id: int | None) -> str:
    if kind not in KINDS:
        raise RemoteError("unsupported_kind", 400)
    if kind == "epub.single.v1":
        if type(novel_id) is not int or novel_id <= 0:
            raise RemoteError("invalid_novel_id", 400)
    elif novel_id is not None:
        raise RemoteError("unexpected_novel_id", 400)
    return digest(canonical_bytes({"kind": kind, "novel_id": novel_id}))


@dataclass(frozen=True)
class Grant:
    run_id: str
    attempt_id: str
    session_id: str
    epoch: str
    fence: int
    input_sha256: str
    kind: str
    lease_until: float
    hard_deadline: float
```

`RemoteConfig` frozen dataclass 字段：enabled/role/state_dir/machine_id/worker_key_sha256/server_origin/work_dir/api_key/worker_key；role 无关字段 None，两个明文密钥 repr=False。角色仅 web/controller/worker，按配置表校验，不在解析时创建目录或调用网络。

测试隔离 fixture：

```python
import os
import socket
import pytest


@pytest.fixture(autouse=True)
def remote_isolation(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("PIXIV_REMOTE_") or name == "INSTABOX_API_KEY":
            monkeypatch.delenv(name, raising=False)

    def deny_connect(*args, **kwargs):
        raise AssertionError("remote tests require injected transports")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)
    monkeypatch.setenv("PIXIV_REMOTE_STATE_DIR", str(tmp_path / "remote"))
    monkeypatch.setenv("PIXIV_REMOTE_WORK_DIR", str(tmp_path / "scratch"))
```

- [ ] **Step 4: GREEN.** 加入 URL、相对目录、配置角色隔离、秘密 repr 和 UTF-8 字节限额测试，运行 Step 2 命令。
- [ ] **Step 5: Commit.** `git add src/pixiv_novel_sync/remote tests/remote; git commit -m "feat: define closed remote execution contracts"`。

## Task 2: Durable Store, Sessions And Fenced Attempts

**Files:** Create `remote/schema.py`, `remote/store.py`, `remote/sessions.py`, `tests/remote/test_store.py`, `tests/remote/test_sessions.py`。

**Interfaces:** `RemoteStore(state_dir: Path, machine_id: str)`；`migrate()`、`close()`。方法使用显式 `now: float` UTC 秒，便于无 sleep 测试；除下面标注外返回 dict。

| 方法 | self 以外参数 | 输出/责任 |
|---|---|---|
| `find_submission` | `key: str, request_sha256: str` | dict/None；同键不同请求冲突 |
| `enqueue` | `key: str, request_sha256: str, kind: str, input_sha256: str, now: float` | run；服务层已冻结输入；总 active cap 20 |
| `enroll` | `boot_id: str, token_hash: str, protocol: int, renderer: str, now: float` | session；同 boot/hash 幂等；退役 boot 不复活 |
| `authenticate` | `token: str, now: float` | active session；不隐式续期 |
| `session_heartbeat` | `session_id: str, now: float` | 会话续至 now+900，不续任务租约 |
| `claim` | `session_id: str, claim_key: str, now: float` | `Grant | None`；原子门控/领取 |
| `check_grant` | `grant: Grant, now: float` | None；检查当前有效授权 |
| `renew` | `grant: Grant, seq: int, now: float` | 固定心跳 receipt，不复活过期 attempt |
| `fail` | `grant: Grant, code: str, now: float` | server 重试分类 |
| `cancel` | `run_id: str, now: float` | CAS 取消，不回滚已成功结果 |
| `sweep` | `now: float` | int；清理租约/会话/截止并有界重试 |
| `get_run` / `list_runs` | `run_id: str` / `limit: int=50` | 脱敏状态，list 返回 list[dict] |
| `rotate_epoch` | `now: float` | str；离线恢复换世代，撤销 session/attempt |

独立 `control.db` 可以复用 `DatabaseConnection` 的连接/事务所有权，但控制连接使用 `synchronous=FULL`、busy timeout 5000 ms。不要改变主库 pragmas，不使用 `Database` 或主库 schema。schema 采用 `PRAGMA user_version=1`；拒绝更新版本，在一个事务中从 0 迁移。普通 Flask/controller 重启不能轮换 epoch。

| 表 | 必需列 |
|---|---|
| `meta` | singleton id, epoch, next_fence |
| `sessions` | session_id, machine_id, boot_id, token_hash, protocol, renderer, state, created_at, expires_at, last_seen_at |
| `runs` | run_id, scope='dashboard', idempotency_key, request_sha256, kind, input_sha256, state, created_at, not_before, deadline, finished_at, error_code |
| `attempts` | attempt_id, run_id, attempt_no, machine_id, session_id, claim_key, epoch, fence, state, claimed_at, lease_until, hard_deadline, heartbeat_seq, heartbeat_receipt_json, ended_at, error_code |
| `artifacts` | attempt_id, chunk_id=0, sha256, size_bytes, blob_ref, state, received_at, published_at |
| `machine_control` | singleton id, revision, machine_id, activation_id, owned_run_started_at, mode, gate_ready_until, runtime_budget_until, hold_until, idle_since, next_poll_at, last_provider_at, provider_state, cooldown_until, last_error, action_json, leader_id, leader_until |
| `blob_objects` | sha256, size_bytes, created_at, kind, state(ready/deleting) |
| `input_parts` | input_sha256, part(manifest/text/cover), blob_sha256, size_bytes |
| `blob_reservations` | reservation_id, purpose(input/result), owner_key, size_bytes, expires_at |

使用外键连接 run/attempt/artifact；state/kind/大小/次数使用 CHECK。时间列 REAL UTC 秒，ID TEXT UUID，所有摘要 64 hex。`runtime_budget_until` 为 null 时禁止领取，明确无限用 `-1`；不可将未知值当无限。`action_json` 只存 controller 固定动作结构，Task 6 定义，不是执行命令。

增加 `UNIQUE(input_sha256,part)` 和 `UNIQUE(purpose,owner_key)`；input_parts 是同一份快照的固定引用清单，不存原始正文。所有新增引用在事务内确认 blob state=ready；GC 标为 deleting 的对象不允许被新任务引用，调用者等清理收敛后重存，而不是一边删除一边借用。

- [ ] **Step 1: Add failing persistence/session tests.**

```python
import hashlib
import uuid
import pytest
from pixiv_novel_sync.remote.contracts import RemoteError
from pixiv_novel_sync.remote.store import RemoteStore


def test_submission_survives_reopen(tmp_path):
    root = tmp_path / "queue"
    store = RemoteStore(root, "fixture-machine")
    store.migrate()
    first = store.enqueue("click-1", "a" * 64, "diagnostic.v1", "b" * 64, 1000)
    store.close()
    reopened = RemoteStore(root, "fixture-machine")
    reopened.migrate()
    assert reopened.find_submission("click-1", "a" * 64)["run_id"] == first["run_id"]
    with pytest.raises(RemoteError, match="idempotency_conflict"):
        reopened.find_submission("click-1", "c" * 64)
    reopened.close()


def test_replaced_boot_cannot_reenroll(tmp_path):
    store = RemoteStore(tmp_path / "queue", "fixture-machine")
    store.migrate()
    boot1, boot2 = str(uuid.uuid4()), str(uuid.uuid4())
    hash1 = hashlib.sha256(b"session-one").hexdigest()
    hash2 = hashlib.sha256(b"session-two").hexdigest()
    old = store.enroll(boot1, hash1, 1, "epub-v1", 1000)
    assert store.enroll(boot1, hash1, 1, "epub-v1", 1001)["session_id"] == old["session_id"]
    store.enroll(boot2, hash2, 1, "epub-v1", 1002)
    with pytest.raises(RemoteError, match="retired_boot"):
        store.enroll(boot1, hash1, 1, "epub-v1", 1003)
    store.close()
```

- [ ] **Step 2: RED.** `python -m pytest tests/remote/test_store.py tests/remote/test_sessions.py -q`。
- [ ] **Step 3: Implement the schema and short transactional transitions.** 这些约束是并发保障，不使用 Python 锁代替：

```sql
CREATE UNIQUE INDEX run_event ON runs(scope, idempotency_key);
CREATE UNIQUE INDEX boot_identity ON sessions(machine_id, boot_id);
CREATE UNIQUE INDEX one_session ON sessions(machine_id) WHERE state = 'active';
CREATE UNIQUE INDEX claim_receipt ON attempts(session_id, claim_key);
CREATE UNIQUE INDEX attempt_order ON attempts(run_id, attempt_no);
CREATE UNIQUE INDEX fence_identity ON attempts(epoch, fence);
CREATE UNIQUE INDEX one_machine_attempt ON attempts(machine_id) WHERE state = 'active';
CREATE UNIQUE INDEX one_run_attempt ON attempts(run_id) WHERE state = 'active';
CREATE UNIQUE INDEX one_run_success ON attempts(run_id) WHERE state = 'succeeded';
```

`claim` 在 `BEGIN IMMEDIATE` 内：验证 session；先查原 claim_key receipt；sweep；检查 controller gate、slot；选最早 eligible queued run；校验 provider 剩余至少 660 秒或明确无限；分配 UUID attempt 并递增 fence；run 置 active；持久化后返回 Grant。不在事务内网络/读写大文件。

gate 要求 provider 确认距今 <=45 秒、runtime.ready、worker 心跳 <=60 秒、协议兼容、mode=ready。无任务返回 None；有旧 claim receipt 就不能换领另一 run，即使该 attempt 已终结。前端 provider 状态不是领取授权。

`check_grant` 比较 DB 中的 session/epoch/fence/input/current attempt/lease/hard deadline，不信任 client 的截止时间。`renew` 相同 seq 返回原 `{seq,lease_until}`，更小 seq 拒绝，更大 seq 才更新 `min(now+180,hard_deadline)`；先判过期再判重放。hard deadline=`min(run.deadline,claimed_at+600)`。

run 状态 queued/active/succeeded/failed/cancelled/expired；attempt 状态 active/succeeded/failed/lost/cancelled/expired；终态不复活。纯计算重试决策加入 `contracts.py`：

```python
def retry_delay(kind: str, attempt_no: int, code: str) -> int | None:
    if kind not in KINDS or attempt_no >= MAX_ATTEMPTS:
        return None
    if code not in {"lease_lost", "worker_restarted", "worker_transient"}:
        return None
    return 30 * (2 ** (attempt_no - 1))
```

deadline 优先于 retry。超时为 expired；输入/格式/协议失败不自动重试。新 session 注册原子退役旧 session 并使其 attempt 失效。保留本 epoch 的退役 boot tombstone，防止旧请求重新夺权；不凭清理历史身份释放容量，必要时显式轮换 epoch。远程会话 secret 只存 hash。

每 epoch 最多 4096 个 boot 记录；到上限拒绝新注册并显示需要维护，不静默删除 tombstone 允许旧 boot 复活。日常会话 heartbeat 不创建新 boot，避免把临时网络波动变成快速身份膨胀。同 boot 但 token_hash 不同是 409，不修改原记录。

- [ ] **Step 4: GREEN and race tests.** 两个独立 store 实例+barrier 竞争领取；同 claim 重试；租约相等时过期；迟到心跳；总三次限制；取消竞争；app 重开 epoch 不变；新 schema 拒绝；主库路径不能被控制库占用。测试 fixture 可直接写控制表建立模拟 ready，不提供生产 HTTP readiness bypass。运行 Step 2 命令。
- [ ] **Step 5: Commit.** `git add src/pixiv_novel_sync/remote tests/remote; git commit -m "feat: persist remote runs and fenced worker leases"`。

## Task 3: Immutable Inputs And Two-Phase Results

**Files:** Create `remote/blobs.py`, `remote/epub_task.py`, `remote/results.py`, `tests/remote/test_blobs.py`, `tests/remote/test_results.py`, `tests/remote/test_epub_task.py`; extend control store only for artifact/quota transactions。

**Interfaces:** `BlobStore(root: Path)`; `put_bytes(data: bytes, limit: int) -> str`, `receive(stream, expected_size: int, expected_sha256: str, limit: int) -> tuple[str,int]`, `open_blob(sha256: str)`；`freeze_epub(novel: dict, cover: bytes | None, blobs: BlobStore) -> str` 返回 manifest hash；`render_epub(manifest: dict, text: bytes, cover: bytes | None, work_dir: Path) -> bytes`；`ResultService(store,blobs)` 的 `stage(grant,stream,size,sha256,now)`, `publish(grant,sha256,size,now)`, `receipt(run_id,session_id,now)` 返回 dict。

- [ ] **Step 1: Add failing round-trip and UTF-8 tests.**

```python
import io
import json
import zipfile
import pytest
from pixiv_novel_sync.remote.blobs import BlobStore
from pixiv_novel_sync.remote.epub_task import freeze_epub, render_epub
from pixiv_novel_sync.remote.contracts import RemoteError


def test_minimal_snapshot_renders_without_business_fields(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    novel = {"novel_id": 42, "title": "Fixture", "author_name": "Author",
             "text_raw": "First page[newpage]Second page", "raw_json": "private-extra"}
    sha = freeze_epub(novel, None, blobs)
    with blobs.open_blob(sha) as source:
        manifest = json.load(source)
    assert "raw_json" not in manifest
    with blobs.open_blob(manifest["text_sha256"]) as source:
        text = source.read()
    result = render_epub(manifest, text, None, tmp_path / "work")
    with zipfile.ZipFile(io.BytesIO(result)) as book:
        assert book.read("mimetype") == b"application/epub+zip"
        assert b"Second page" in b"".join(book.read(n) for n in book.namelist() if n.endswith(".xhtml"))


def test_text_limit_is_bytes(tmp_path):
    novel = {"novel_id": 1, "title": "Large", "author_name": "A",
             "text_raw": "\u4e2d" * (6 * 1024 * 1024)}
    with pytest.raises(RemoteError, match="text_too_large"):
        freeze_epub(novel, None, BlobStore(tmp_path / "blobs"))
```

- [ ] **Step 2: RED.** `python -m pytest tests/remote/test_blobs.py tests/remote/test_results.py tests/remote/test_epub_task.py -q`。
- [ ] **Step 3: Implement immutable blobs, snapshot whitelist and rendering.**

```python
from pathlib import Path
from pixiv_novel_sync.epub_exporter import create_epub_from_novel
from pixiv_novel_sync.remote.contracts import (
    PROTOCOL, RENDERER, TEXT_LIMIT, COVER_LIMIT, RESULT_LIMIT,
    RemoteError, canonical_bytes, digest,
)


def freeze_epub(novel: dict, cover: bytes | None, blobs) -> str:
    text = str(novel.get("text_raw") or "").encode("utf-8")
    if not text:
        raise RemoteError("text_missing", 400)
    if len(text) > TEXT_LIMIT:
        raise RemoteError("text_too_large", 413)
    if cover is not None and len(cover) > COVER_LIMIT:
        raise RemoteError("cover_too_large", 413)
    manifest = {
        "protocol": PROTOCOL, "renderer": RENDERER,
        "novel_id": int(novel["novel_id"]),
        "title": str(novel.get("title") or ""),
        "author_name": str(novel.get("author_name") or ""),
        "text_sha256": digest(text), "text_size": len(text),
        "cover_sha256": digest(cover) if cover is not None else None,
        "cover_size": len(cover) if cover is not None else 0,
    }
    encoded = canonical_bytes(manifest)
    if len(encoded) > 64 * 1024:
        raise RemoteError("manifest_too_large", 413)
    blobs.put_bytes(text, TEXT_LIMIT)
    if cover is not None:
        blobs.put_bytes(cover, COVER_LIMIT)
    return blobs.put_bytes(encoded, 64 * 1024)


def render_epub(manifest: dict, text: bytes, cover: bytes | None, work_dir: Path) -> bytes:
    if manifest["protocol"] != PROTOCOL or manifest["renderer"] != RENDERER:
        raise RemoteError("protocol_mismatch")
    if len(text) > TEXT_LIMIT or len(text) != manifest["text_size"] or digest(text) != manifest["text_sha256"]:
        raise RemoteError("input_mismatch")
    cover_path = None
    if manifest["cover_sha256"] is not None:
        if cover is None or len(cover) > COVER_LIMIT or len(cover) != manifest["cover_size"] or digest(cover) != manifest["cover_sha256"]:
            raise RemoteError("input_mismatch")
        work_dir.mkdir(parents=True, exist_ok=True)
        cover_path = work_dir / "cover.bin"
        cover_path.write_bytes(cover)
    elif cover is not None:
        raise RemoteError("unexpected_cover")
    metadata = {key: manifest[key] for key in ("novel_id", "title", "author_name")}
    result = create_epub_from_novel(metadata, text.decode("utf-8"), cover_path)
    if len(result) > RESULT_LIMIT:
        raise RemoteError("result_too_large", 413)
    return result
```

`BlobStore` 路径完全由内部 SHA-256 生成；临时文件在专用 root 下用随机名，流式每次最多 64 KiB。接收实际大小超 declared size/limit 即中止；不信任 Content-Length 或摘要。写后 flush/fsync，再原子替换内容地址文件，Linux fsync 父目录，之后才返回。已存在同 hash 时核对长度/摘要，不覆盖可疑文件。禁止 symlink root/对象路径逃逸；只删除核验位于专用目录的已知文件，不递归删除用户路径。

先在短事务预留文件总配额：输入按有界正文/封面/清单总量预留，结果按声明输出量预留；相同 owner_key 同时只允许一个上传写入。流式文件写入/格式校验在事务外，完成后再校验 grant 并登记 ready blob/staged artifact。所有失败路径释放 reservation；磁盘 ENOSPC 返回固定错误，任务不得成功。输入也必须先落盘、登记 input_parts 再 enqueue；并发同键只采用第一次发布的输入，未采用的无引用 blob 由 GC 收回，不需要跨业务库事务。

在 RemoteStore 增加 `reserve_blob(purpose: str, owner_key: str, size_bytes: int, now: float) -> str`、`release_reservation(reservation_id: str) -> None`、`register_input(input_sha256: str, parts: dict[str,tuple[str,int]], reservation_id: str, now: float) -> None`、`register_staged(grant: Grant, sha256: str, size_bytes: int, reservation_id: str, now: float) -> dict`。BlobStore 不自行信任调用者的配额；生产 submit/ResultService 必须通过这些事务接口，低层测试可直接测试文件写入。reservation 最多 180 秒，输入构造/上传超过时限失败后重试，不能变成永久配额占用。

EPUB 验证：不解压到磁盘；拒绝重复/绝对/反斜杠/`..` 成员、symlink、加密、未知压缩格式；最多 4096 成员、总解压大小 128 MiB，逐流实际解压计数及 CRC 校验；必须有 mimetype=`application/epub+zip`、META-INF/container.xml。诊断 JSON 严格匹配 challenge/版本且 <=4 KiB。没有任何 XML 外部实体或网络加载。

`publish` 在一条队列库事务中再次检查 grant、固定 input hash 与 staged hash/size，然后 artifacts->published、attempt->succeeded、run->succeeded。响应丢失后，同样的已成功 manifest 返回原 receipt，不重新检查已终结的 lease；仍要求当前有效 session 且属于同一配置机器。不同 hash 409，旧失效 attempt 不得借此发布。下载只允许主机本地的已发布 blob。

- [ ] **Step 4: GREEN and crash matrix.** 注入写到一半、fsync 后/DB 前、commit 后/response 前三处故障；重复 stage/publish；取消或过期发生在上传中；伪 hash、恶意 ZIP、配额竞争；确认没有成功记录引用残缺文件。比较本地/远程 EPUB 内容语义，不比较带生成时间的 ZIP 字节。运行 Step 2 命令以及 `python -m pytest tests/test_epub_exporter.py -q`。
- [ ] **Step 5: Commit.** `git add src/pixiv_novel_sync/remote tests/remote; git commit -m "feat: stage and publish bounded remote artifacts"`。

## Task 4: Authenticated HTTP Integration

**Files:** Create `remote/web.py`, `remote/worker_api.py`, `tests/remote/test_web.py`, `tests/remote/test_worker_api.py`; modify `webapp.py` only at auth guard and register-routes area。

**Interfaces:** `register_remote_routes(app, settings_callback) -> None`; `create_worker_blueprint(store, blobs, results, cfg, clock) -> Blueprint`。注册仅在 enabled 时开 worker/API，页面可显示关闭；函数不启动线程、不调用 provider。用户提交先 `find_submission`，缺失才读主库、冻结输入、enqueue；原子 enqueue 再次检查同 key 防竞争。

HTTP 层通过新增 `RemoteStore.resolve_grant(session_id: str, attempt_id: str, epoch: str, fence: int, input_sha256: str, now: float) -> Grant` 从持久记录重建授权并调用 check_grant；不能把客户端提交的 run_id/deadline 当真。publish 的已成功 receipt 查询单独认证，不使用这条只允许 active 的校验路径。claim 重放若原 attempt 已终结返回 409 `claim_terminal` 并附原 run_id/attempt_id，仅供查 receipt，不派另一个任务。

- [ ] **Step 1: Add actual-auth negative tests.** 使用临时空 env、missing.yaml、固定测试密码和 `start_scheduler=False`；所有客户端设置 RAW_HTTP=True，避免根 conftest 自动代填 CSRF。

```python
from pixiv_novel_sync.webapp import create_app


def test_worker_credentials_are_not_dashboard_login(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-dashboard-password")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "test-only-secret")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    app = create_app(config_path=str(tmp_path / "missing.yaml"),
                     env_path=str(env), start_scheduler=False)
    app.config.update(TESTING=True, RAW_HTTP=True)
    client = app.test_client()
    response = client.get("/api/dashboard/sync/status",
                          headers={"Authorization": "Bearer worker-test-token"})
    assert response.status_code == 401
```

补充 enabled fixture 初始化 RemoteConfig/控制库、bootstrap 摘要和临时 library 数据。真实登录通过 `/api/auth/login`，再 GET `/api/csrf-token`；不要伪造 `session['authenticated']=True`。

- [ ] **Step 2: RED.** `python -m pytest tests/remote/test_web.py tests/remote/test_worker_api.py -q`。必需新增断言：有效 worker 请求无需 Dashboard cookie；没有 worker token 即便 Dashboard 已登录也 401；用户 POST 无 CSRF 403；未知前缀路径不能绕认证。
- [ ] **Step 3: Register narrowly scoped auth and routes.** 在 `webapp.py:_check_auth` 中，现有 rescue 特例旁仅增加：

```python
if request.blueprint == "remote_worker":
    return None
```

这不是无认证放行：`create_worker_blueprint` 必须无条件安装自己的 `before_request`，认证失败关闭。任何新 worker endpoint 都在同一 hook 下；OPTIONS 也不放行跨站调用。未知路径无该 blueprint 身份。Dashboard remote 入口不属于 worker blueprint，继续原 auth/CSRF。

在 `current_settings_for_routes` 与现有 register 函数区域增加：

```python
from .remote.web import register_remote_routes
register_remote_routes(app, current_settings_for_routes)
```

bootstrap 比较使用 `hmac.compare_digest(sha256(provided),configured_hash)`；session 验证只查控制库。注册请求只收固定字段，新的 boot 原子撤销旧 session。普通 session 不能调用 bootstrap 注册。所有 token 均不在 query/body 回显或日志中；注册 body 中 session_secret 不得进请求日志。

HTTP error mapping：400 schema/unknown fields，401 token/session，403 scope，404 无对象，409 冲突/过期 grant，410 清理后的结果，413 限额，429 请求频率/排队上限，503 存储暂不可用。默认仅 JSON `{error:code}`，不回传异常栈；唯一额外字段例外为已认证 claim_terminal 的原 run_id/attempt_id，供核对而非领取授权。

用户读主库时只读必要小说字段，封面经现有 `FileStorage.get_novel_cover_path` 定位并核验在配置归档根内；禁止任意路径/远程封面 URL，读字节数不超过 5 MiB。freeze/publish 不更新主库或主 task_logs。用户页面及 API 读取服务，不触发实例操作。

- [ ] **Step 4: GREEN/regression.** 同键两次提交之间更新主库正文仍返回原 run/input；跨 attempt、过期 token、字段注入、Cookie/Origin、路径参数、未知 blueprint、匿名下载均拒绝；secret/log 检查。运行 Step 2 命令和 `python -m pytest tests/test_mobile_auth.py tests/test_webapp_security.py tests/test_webapp_jobs.py tests/test_epub_exporter.py tests/test_test_isolation.py -q`。
- [ ] **Step 5: Commit.** `git add src/pixiv_novel_sync/remote src/pixiv_novel_sync/webapp.py tests/remote; git commit -m "feat: expose isolated authenticated worker APIs"`。

## Task 5: InstaBox Adapter With Verified Wire Contracts

**Files:** Create `remote/instabox.py`, `tests/remote/test_instabox.py`, `tests/remote/fixtures/{machine,operation,cooldown}.json`。

**Interfaces:** `InstaBoxClient(api_key: str, machine_id: str, transport=None)`；`machine() -> MachineView`、`operation(operation_id: str) -> OperationView`、`mutate(action: str, idempotency_key: str) -> OperationView`、`wake() -> MachineView`。action 只 start/resume/suspend；生产 base 固定 `https://api.instabox.sh/v1`，测试只替换 transport，不开放任意 base URL。

`MachineView` frozen dataclass 字段 id/state/operation_id/runtime_ready/max_run_minutes/run_started_at/cooldown_until；`OperationView` 字段 id/machine_id/action/state/error_code。`ProviderError` 包含 code/http_status/retry_after/operation_id，只含脱敏字段。

已核对的公开契约：

| 请求 | 成功 | 注意 |
|---|---|---|
| GET `/machines/{id}` | 200 裸 Machine | 不是 `{machine:...}` |
| POST `/machines/{id}/start` | 202 裸 Operation | `{}`、Idempotency-Key；支持 stopped/suspended |
| POST `/machines/{id}/resume` | 202 裸 Operation | `{}`、Idempotency-Key |
| POST `/machines/{id}/suspend` | 202 裸 Operation | 不是 `/pause`；`{}`、Idempotency-Key |
| GET `/operations/{id}` | 200 裸 Operation | 普通短轮询；一期不用 25 秒长等阻塞心跳 |
| POST `/machines/{id}/wake` | 200 裸 Machine | `{}`；不要求幂等键，不作为启动完成证据 |

wire machine 状态：creating/starting/running/stopping/stopped/suspending/suspended/resuming/deleting/deleted/error。Operation 为 queued/running/succeeded/failed。provider state 不能写成 paused/failed。`runtime.ready` boolean，`maxRunMinutes` int，`runStartedAt/cooldownUntil` RFC3339 或 null；unknown state/缺字段/类型错 fail closed。

认证用 X-API-Key；POST start/resume/suspend 幂等键 1–200 可打印 ASCII 无空格，同 method/path/body 重试保持原键。controller 持久 action 后才发请求；adapter 不偷偷 retry 改状态请求。禁止 redirect，TLS 校验开启，connect/read timeout=(5,10)，响应最多 1 MiB。

错误 envelope 为 `{error:{code,message,retryAfterSeconds,operationId}}`，辅助字段允许缺失；不能把缺失 retryable 当 true。结合 HTTP Retry-After（秒或 HTTP 日期）、error.retryAfterSeconds 和 Machine.cooldownUntil 选最晚可重试时刻；所有 409 不等于 cooldown，operation_conflict 要跟踪其 operationId。

一期用已有精确契约的 running-only wake 防空闲，每 5 分钟最多一次，只在真实工作需求或未过期人工 hold 下调用；hold 不启动已经停止/暂停的实例。**不实现 activity-leases API**：其方法/TTL 已公开但本次未完整确认响应契约，没必要为一期引入未核实接口；也不使用 exec 启动业务。最大运行时间不能被 wake 延长。

- [ ] **Step 1: Add pure decoder/transport tests.** fixtures 是文档 schema 构造的数据，不是账户抓包。机器样本只需 decoder 消费字段，其余 schema 字段可保留在完整 JSON fixture 中。

```python
import pytest
from pixiv_novel_sync.remote.instabox import decode_machine, ProviderError


def test_running_is_not_runtime_ready():
    view = decode_machine({
        "id": "m_fixture", "state": "running", "operationId": None,
        "runtime": {"ready": False}, "maxRunMinutes": 480,
        "runStartedAt": "2026-11-10T12:50:00Z", "cooldownUntil": None,
    }, expected_id="m_fixture")
    assert view.runtime_ready is False


@pytest.mark.parametrize("state", ["paused", "failed", "unknown"])
def test_unknown_wire_states_fail_closed(state):
    with pytest.raises(ProviderError, match="invalid_machine"):
        decode_machine({"id": "m_fixture", "state": state}, expected_id="m_fixture")
```

再测试 bare Operation 没有可选 machine 仍可解码，错 machineId 拒绝；start 的 URL/body/header 固定，timeout 不换幂等键；429 Retry-After；409 cooldown vs conflict；unknown state、false ready、run time 空缺不默认无限。

- [ ] **Step 2: RED.** `python -m pytest tests/remote/test_instabox.py -q`。
- [ ] **Step 3: Implement requests adapter and strict parsing.** 日期解析使用 aware UTC，拒绝无时区；bool 不是 int；`maxRunMinutes` 仅 -1 或 >=0，0 不可运行。仅 -1 明确无限；有限额度但 runStartedAt 为 null 时 remaining unknown。异常正文不打印。fixture cooldown：

```json
{
  "error": {
    "code": "machine_cooldown",
    "message": "fixture cooldown",
    "requestId": "req_fixture",
    "retryable": true,
    "operationId": null,
    "retryAfterSeconds": 900
  }
}
```

`decode_machine(payload: dict, expected_id: str) -> MachineView`、`decode_operation(payload: dict, expected_machine_id: str) -> OperationView` 使用固定状态集合及必需类型检查；operation failed 只取 error.code。transport `.request()` 参数完全由 adapter 构造；不让 worker 指定路径/headers。

- [ ] **Step 4: GREEN.** 运行 Step 2，验证网络禁止 fixture 确实能捕获未注入 transport 的意外请求。将官方 URL 放 fixture 说明，不把测试通过称为真实云验证。
- [ ] **Step 5: Commit.** `git add src/pixiv_novel_sync/remote/instabox.py tests/remote; git commit -m "feat: add bounded InstaBox lifecycle adapter"`。

## Task 6: Demand-Driven Controller And Safe Draining

**Files:** Create `remote/control_store.py`, `remote/lifecycle.py`, `tests/remote/test_lifecycle.py`；extend control schema only if fields already listed need implementation。

**Interfaces:** `ControlStore(store: RemoteStore)` 的 `snapshot(now) -> dict`、`set_hold(seconds,now) -> dict`、`observe(machine: MachineView,now) -> None`、`claim_leader(owner,now) -> bool`、`reserve_action(action,activation_id,now) -> dict`、`record_action(action_id,response,now) -> None`；`Controller(store,control,provider,clock,jitter)` 的 `tick() -> float` 返回建议下一 tick UTC 时间。所有 clock/jitter 可注入。

leader lease 60 秒，每 tick 15 秒；provider HTTP 超时每次最多 15 秒且每 tick 只做一个动作，action idempotency/操作记录在 worker gate 之前优先核对。持久 `action_json` 固定字段：action_id/action/idempotency_key/operation_id/activation_id/phase(created,sent,accepted,settled,unknown)/created_at/last_sent_at/retry_at。不保存 secret/body 任意文本。

mode 为 disabled/idle/starting/ready/draining/suspending/cooldown/error。owned activation 只有成功由该 controller 发起 start/resume 并观察新 runStartedAt 后成立；已 running 或无法证明归属时 ownership=null，只借用执行、不自动暂停。

- [ ] **Step 1: Add stateful fake provider tests.** `FakeProvider` 保留 MachineView、动作列表、OperationView，默认每次 mutate 只受理不自动成功；测试显式推进 operation/machine。测试关键行为：

```python
def test_no_demand_does_not_wake(controller_fixture):
    f = controller_fixture
    f.provider.set_state("suspended", runtime_ready=False)
    f.controller.tick()
    assert f.provider.mutations == []
    assert f.provider.wake_count == 0


def test_manual_running_instance_is_not_suspended(controller_fixture):
    f = controller_fixture
    f.provider.set_state("running", runtime_ready=True)
    f.controller.tick()
    f.clock.now += 1000
    f.controller.tick()
    assert "suspend" not in f.provider.mutations


def test_accepted_start_does_not_open_claim_gate(controller_fixture):
    f = controller_fixture
    f.enqueue_diagnostic()
    f.controller.tick()
    assert f.control.snapshot(f.clock.now)["mode"] == "starting"
    assert f.store.claim(f.session_id, "claim-before-ready", f.clock.now) is None
```

`controller_fixture` 在本测试文件定义：Clock(now=1000)、临时 RemoteStore、ControlStore、FakeProvider、Controller、已 enroll session，`enqueue_diagnostic` 用 Task 3 BlobStore 生成输入；无真实 provider 或 sleep。fixture 设置 machine suspended，gate 关闭，未人为 ownership。

- [ ] **Step 2: RED.** `python -m pytest tests/remote/test_lifecycle.py -q`。
- [ ] **Step 3: Implement tick using this ordered decision table.** 同一实例一次仅一个在途 operation，网络在事务外，接收响应时 CAS action_id/revision，过时回执不改变当前状态。

| 顺序 | 条件 | 动作 |
|---|---|---|
| 1 | 非 leader 或 disabled | 不写 provider；不延长 gate |
| 2 | 过期 runs/sessions/attempts | sweep；重新计算有效需求 |
| 3 | 既有 pending action/operation | 查询并收敛，完成前不派新业务、不发相反动作 |
| 4 | cooldown/429/backoff 尚未到 | 等待，显示原因，deadline 继续有效 |
| 5 | 有需求、stopped/suspended | 先持久 start/resume intent+幂等键，关闭 gate，再发送 |
| 6 | transitional 或 runtime 未 ready | 保持 gate 关闭，轮询；不重复 start |
| 7 | running/runtime ready/worker compatible+新鲜 | gate 有效至 now+45；记录预算；允许 claim |
| 8 | 有 active/eligible pending 需求或未过期 hold，且运行中 | 最多每 300 秒 wake 一次；不延长 max-run |
| 9 | 无需求且未 hold | 记录 idle_since；>=300 秒进入 draining、关 gate |
| 10 | draining，仍无需求/active/upload 且拥有本次运行 | 同事务重查并预留 suspend action，然后网络发送 |
| 11 | 发现人工运行、身份变化或无法确认 ownership | relinquish ownership，不自动 suspend |

暂停前检查 hold 和新的入队需求，若 action 未发取消 draining；已发则等暂停 operation 收敛后再恢复，不能边等旧 suspend 边 start。hold 是 inhibit，不会强行撤销已发操作；界面如实显示“暂停请求已发，恢复等待中”。

网络超时：同一 pending lifecycle action 重试沿用幂等键，优先根据 machine.operationId/已知 operationId 查询；7 天幂等保留期之外不自动重放。未知 operation 不能按成功处理。401/403 配置错误停止 mutation 并告警；5xx/传输错误退避 30/60/120...上限 300 秒并注入 0–5 秒 jitter。409 machine_cooldown 等 actual deadline；operation_conflict 跟踪具体 operationId。每分钟 lifecycle 写操作不超过 2 次，以保守避开免费共享限流；wake 也算写操作。

controller 成功 leader lease 过期并不使已发远端调用消失；保留 action intent，下一 leader 只能继续核对同一 action。无法证明归属的 suspend 超时/人工干预，进入 error/需确认，不声称平台支持本地 fencing 条件写入。

- [ ] **Step 4: GREEN and race matrix.** 新任务发生在 draining/发送 suspend 后、hold 同时写入、VPS 重启丢 HTTP 响应、冷却递增、未知 state、已有人工 running、remaining budget 不足、provider ready 但 worker 不在线、旧 operation 延迟回执；保证没有错误成功/双操作。运行 Step 2。
- [ ] **Step 5: Commit.** `git add src/pixiv_novel_sync/remote tests/remote; git commit -m "feat: coordinate demand-driven cloud lifecycle"`。

## Task 7: Dedicated Worker And Conservative Lease Supervision

**Files:** Create `remote/worker.py`, `remote/worker_http.py`, `remote/runner.py`, `tests/remote/test_worker.py`, `tests/remote/test_runner.py`。

**Interfaces:** `WorkerHTTP(origin,key,transport=None)` 的 `enroll(boot_id,session_secret)`, `session_heartbeat()`, `claim(key)`, `fetch_input(grant,part)`, `renew(grant,seq)`, `stage(grant,path,sha256,size)`, `publish(grant,sha256,size)`, `fail(grant,code)`, `receipt(run_id)`；`Worker(client,work_dir,clock,monotonic,runner)` 的 `tick() -> float`, `stop()`；`run_child(kind,manifest_path,text_path,cover_path,output_path)` 仅被固定 multiprocessing target 调用，不从网络接受这些路径。

JSON 方法返回 dict；claim 返回 `Grant | None`，终结 receipt 通过 typed RemoteError `claim_terminal` 附原 run_id/attempt_id 表示；fetch_input 返回有长度上限的 bytes。HTTP 错误只保存 code/status/允许的原任务 ID，不把任意 error body 放日志。每次新 attempt 创建独立 LeaseGuard，不复用上一 attempt 的 safe_until。

- [ ] **Step 1: RED tests for conservative deadline and isolation.**

```python
from pixiv_novel_sync.remote.worker import LeaseGuard


def test_delayed_renew_response_cannot_grant_extra_time():
    guard = LeaseGuard()
    guard.accept(sent_monotonic=100.0, received_monotonic=219.0)
    assert guard.can_work(219.0)
    assert not guard.can_work(220.0)


def test_response_after_safe_budget_is_not_work_permission():
    guard = LeaseGuard()
    guard.accept(sent_monotonic=100.0, received_monotonic=225.0)
    assert not guard.can_work(225.0)
```

补充 `run_child` 用合成 EPUB 输入时 monkeypatch 所有网络连接/Database/create_app 调用为 raise；远程输入中出现封面 URL、SQL、命令字段被协议层拒绝。测试 parent 假 renderer 卡住时仍续 heartbeat，硬截止能够结束子进程。运行 `python -m pytest tests/remote/test_worker.py tests/remote/test_runner.py -q`。

- [ ] **Step 2: Implement LeaseGuard and fixed runner dispatch.**

```python
from dataclasses import dataclass
from pixiv_novel_sync.remote.contracts import STOP_SECONDS


@dataclass
class LeaseGuard:
    safe_until: float = 0.0

    def accept(self, *, sent_monotonic: float, received_monotonic: float) -> None:
        if received_monotonic < sent_monotonic:
            raise ValueError("invalid_monotonic_order")
        self.safe_until = max(self.safe_until, sent_monotonic + STOP_SECONDS)

    def can_work(self, now_monotonic: float) -> bool:
        return now_monotonic < self.safe_until
```

只有匹配当前 Grant、最新 seq、有效 server receipt 的响应可调用 accept；不能让旧 attempt 的迟到回复延长当前 guard。同一 heartbeat seq 的 HTTP 重传必须保留首次发送的单调时刻，不能用最后一次重发时刻延长 120 秒预算，因为服务端会返回原 receipt 而不续租。初次 claim 同样从该 claim_key 首次请求发送时刻算；response 到达已过安全期限则不启动子进程。所有 worker JSON 响应额外带 `server_now`，hard deadline 用其余量转换为本地单调 deadline并扣除整次请求耗时，取更早者，不依赖两个系统时钟一致。

进程启动生成新 boot UUID/随机 session secret，显式握手；会话只允许配置 origin，request 禁止 redirect、TLS 必检。连接中断或本地检测到长暂停（wall elapsed 与 monotonic 差异、tick 超过安全期限）时停止子进程并重新握手；不凭内存继续旧任务。

取任务后下载 manifest/text/cover，流式校验长度/hash/总量到私有工作目录，心跳线程独立于下载/渲染/上传；逐请求 timeout，120 秒未确认即停止工作。输入拿齐前也需续租。renderer 固定 `epub.single.v1` 调 Task 3，diagnostic 仅固定 JSON；未知 task 失败，不 fallback 到 shell。child 使用 `multiprocessing.get_context('spawn')`，无用户任意 target；输出只写 parent 指定的 task 目录。

子进程不需要业务/管理 key；父进程只持 worker secret。执行结束后检查输出上限/hash，stage，再 publish，只有 VPS receipt 成功才算完成并清临时目录。失去 publish 回执先查 receipt；旧 Grant 被拒绝不重新上传至新 attempt；重试由 VPS 分配。任务目录清理用经过 root containment 校验的 pathlib/本平台原生操作，不按用户字符串拼 shell。

失败 code 仅 render_failed/input_mismatch/protocol_mismatch/worker_transient/result_too_large；网络丢失不能由 worker 擅自宣布业务成功。parent 停止时 terminate/join 自己创建的 child，绝不杀系统其他进程。

- [ ] **Step 3: GREEN.** 运行 `python -m pytest tests/remote/test_worker.py tests/remote/test_runner.py -q`；覆盖乱序 heartbeat、过期 upload、断线、provider 强制暂停、冷启动旧 boot、结果响应丢失、session 续期和 max 10 分钟。
- [ ] **Step 4: Import/secret audit.** `remote/runner.py` 不导入 Database/Flask/auth；worker 不调用主 `cli.main`/load_settings；测试日志捕获确认无正文/token。运行 `python -m compileall -q src/pixiv_novel_sync/remote`。
- [ ] **Step 5: Commit.** `git add src/pixiv_novel_sync/remote tests/remote; git commit -m "feat: run bounded cloud workers with lease supervision"`。

## Task 8: Explicit Pilot Page And Honest Status

**Files:** Create `src/pixiv_novel_sync/templates/dashboard_remote.html`, `tests/remote/test_remote_page.py`; modify `templates/base.html`, `remote/web.py`。

**Interfaces:** 页面仅消费 Fixed Contracts 的用户 API，不接触 provider/worker secret。旧小说导出按钮不变；导航新增“远程执行”入口，沿用 Library OS 组件、布局和已有 csrfFetch。

- [ ] **Step 1: Add failing page/API tests.** 登录前页面重定向；关闭时显示未启用且无提交按钮；启用后显示诊断按钮、单本小说 ID 表单、容量/冷却/hold 和列表；无 secret/path/body。

```python
def test_page_is_explicit_and_does_not_replace_downloads(remote_web_fixture):
    f = remote_web_fixture
    f.login()
    page = f.client.get("/dashboard/remote")
    assert page.status_code == 200
    assert "远程执行" in page.get_data(as_text=True)
    assert "单本 EPUB" in page.get_data(as_text=True)
    assert "/api/dashboard/novels/export-epub" in {
        rule.rule for rule in f.app.url_map.iter_rules()
    }
    assert f.provider_calls == []
```

`remote_web_fixture` 复用 Task 4 临时 app，`.login()` 真正表单登录和获取 CSRF，`provider_calls` 为禁止 Web 调 provider 的 fake 记录；不要创建真实生命周期线程。RED：运行 `python -m pytest tests/remote/test_remote_page.py -q` 看到新页面断言失败。

- [ ] **Step 2: Implement page interactions.** 继承 `base.html`，表单调用 `window.csrfFetch`；状态文本用 `textContent` 或模板自动转义，不插入未可信 HTML。提交前生成并保留当前操作幂等键，网络重试沿用，收到确定 run_id 或用户明确新建才换键：

```javascript
let submission = null;
async function submitRemote(kind, novelId) {
  const body = kind === 'epub.single.v1'
    ? {kind, novel_id: Number(novelId)} : {kind};
  const serialized = JSON.stringify(body);
  if (!submission || submission.body !== serialized) {
    submission = {body: serialized, key: crypto.randomUUID()};
  }
  const response = await window.csrfFetch('/api/dashboard/remote/runs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Idempotency-Key': submission.key},
    body: submission.body
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'remote_submit_failed');
  submission = null;
  return data;
}
```

一次点击等待中禁用提交防双击；网络不明不清 key；用 sessionStorage 保存未确认请求和 key，刷新后允许“重试原请求”，不存 token/正文。只有用户输入变更才开始另一事件，明确提示未确认请求仍可能已提交。下载 GET 只在 can_download 时显示；401 明确提示重新登录。

状态分开显示 run.state 和 machine.mode/provider.state，显示 UTC 数据转换的北京时间、last-confirmed age、cooldown/等待/租约恢复，而不是把 running 展示为任务成功。已清理产物显示“已过期”；一期不展示不可用的签到按钮。hold 为 0/1h/4h，POST 后显示实际状态；说明不能撤销已发出的暂停。

- [ ] **Step 3: GREEN.** `python -m pytest tests/remote/test_remote_page.py tests/remote/test_web.py -q`。检查 secret 不在 HTML/JSON，POST 仍需 CSRF，错误码映射不泄漏 traceback。
- [ ] **Step 4: Visual verification.** 使用本地 fake app、合成书目，在 390px/1280px 宽度检查无横向溢出、按钮可触、长错误/标题可换行、等待与关闭状态。沿用现有视觉，不引入新字体/CDN。若增加 Tailwind class，执行现有构建并跑 `python scripts/build_tailwind_css.py --check`；这不是连接生产网站。
- [ ] **Step 5: Commit.** `git add src/pixiv_novel_sync/templates/base.html src/pixiv_novel_sync/templates/dashboard_remote.html src/pixiv_novel_sync/remote/web.py tests/remote; git commit -m "feat: expose remote execution pilot controls"`。

## Task 9: CLI, Services, Limits And Recovery

**Files:** Create `remote/cli.py`, `deploy/systemd/pixiv-remote-controller.service`, `deploy/systemd/pixiv-remote-worker.service`, `config/remote-web.env.example`, `config/remote-controller.env.example`, `config/remote-worker.env.example`, `config/nginx/remote-worker-location.conf`, `docs/REMOTE_EXECUTION.md`, `tests/remote/test_cli.py`, `tests/remote/test_deployment_contract.py`; modify `pyproject.toml`。

**Interfaces:** New entrypoint `pixiv-remote = "pixiv_novel_sync.remote.cli:main"`；commands `controller`, `worker`, `doctor`, `recover`, `gc`；各自 `--once` 仅 controller/worker 支持，doctor 默认离线只查配置/权限/目录，不自动调 API。不接进旧 cli.main，避免全量业务配置加载。

- [ ] **Step 1: Add failing CLI/service contract tests.** parser 不允许 `--command`/任意 shell；worker 调用不导入主 Settings；doctor 不启动服务/联网；disabled controller 不建目录。systemd 文件名称必须不同于旧 pixiv-novel-sync.service/timer。测试 recover 在控制器仍活跃时拒绝，成功轮换后旧 session/grant 401/409。

```python
def test_doctor_never_uses_cloud_key_or_network(monkeypatch, capsys):
    from pixiv_novel_sync.remote.cli import run_cli
    assert run_cli(["doctor"], env={}) == 0
    output = capsys.readouterr().out
    assert "disabled" in output
    assert "INSTABOX_API_KEY=" not in output
```

RED：运行 `python -m pytest tests/remote/test_cli.py tests/remote/test_deployment_contract.py -q`，预期缺失新 CLI 或对应命令，不能因读取真实环境而失败。

- [ ] **Step 2: Implement CLI boundaries and packaging.** `run_cli(argv: list[str], env: Mapping[str,str] | None=None) -> int`；`main()` 使用 `SystemExit(run_cli(sys.argv[1:]))`。命令根据 role 加载 RemoteConfig，依赖延迟导入，只有 controller 会构造 InstaBoxClient。循环使用 stop Event.wait 而非忙循环，SIGTERM 停发任务并关闭自己资源，不立即停云实例。

`recover --rotate-epoch` 要求 remote API 已关闭/服务停用且无 controller leader；明确确认本地目标目录，不接受下载的数据库路径。事务轮换 epoch、撤销 sessions/attempts、所有 gates 关闭，清理控制 ownership 为未知，旧在途 provider operation 仍保留并核对，不能因恢复备份就假装没有旧 suspend。生成新的 bootstrap key/hash 由运维独立步骤执行，不将秘密写 stdout 日志。与旧实例不确定副作用相关的未来任务不自动恢复；一期仅纯计算允许重新排队。

`gc` 使用短事务标记无引用 blob 为 deleting，文件删除后回写，允许重入；新的引用必须拒绝 deleting 对象，防止检查后再引用的删除竞态。依据 input_parts、run/artifact、reservation 和终态保留期，不删除 active 输入/staged，不扫描主归档。无登记的临时/孤儿文件保留至少 1 小时后按专用目录路径校验清理。数据库元数据/文件总量和过期 tombstone 分开管理；普通 GC 不删除本 epoch boot tombstones。

pyproject `[project.scripts]` 追加：

```toml
pixiv-remote = "pixiv_novel_sync.remote.cli:main"
```

- [ ] **Step 3: Add installable but not auto-enabled service examples.** controller 使用 EnvironmentFile 专用配置；worker 使用另一专用配置与非 root 用户。两个 service 独立，部署路径在手工安装前核对，不运行 deploy.sh，不复用旧 oneshot timer。

```ini
[Unit]
Description=Pixiv remote instance controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pixivsync
WorkingDirectory=/opt/pixiv-novel-sync/app
EnvironmentFile=/etc/pixiv-remote/controller.env
ExecStart=/opt/pixiv-novel-sync/app/.venv/bin/pixiv-remote controller
Restart=on-failure
RestartSec=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

worker unit 同样完整创建，Description 改 Pixiv remote worker，EnvironmentFile `/etc/pixiv-remote/worker.env`，ExecStart 最后参数 worker，WorkingDirectory 与安装虚拟环境路径经部署核对；`Restart=always`, `RestartSec=15`, `KillMode=control-group`, `TimeoutStopSec=30`, `UMask=0077`, `NoNewPrivileges=true`, `PrivateTmp=true`。worker 非 root、工作目录只属该用户；进程内冷启动自恢复而非依赖 GUI 登录。

env examples 所有 secret 空白并注释安全生成/安装方法；remote 默认关闭；路径使用明确专用 `/var/lib/pixiv-remote`，worker `/var/lib/pixiv-remote-worker`；实际用户实例系统/权限尚未核对，不写“已安装”。Web 账户与 controller 对控制目录共享最小必要权限，不给 nginx 读秘密。

Nginx 新 location 样例（部署时确认上游端口，不覆盖整个现有配置）：

```nginx
location ^~ /api/remote-worker/v1/ {
    client_max_body_size 33m;
    client_body_timeout 60s;
    proxy_pass http://127.0.0.1:5011;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 10s;
    proxy_read_timeout 75s;
    proxy_send_timeout 75s;
    proxy_cache off;
}
```

应用仍按 endpoint 执行 16 KiB/32 MiB 限额，不能靠 Nginx 的 33m 代替。设置 endpoint 级 request 最大长度并按实际流读取限制；检查 WSGI 不把无 Content-Length 的 body 无限读入。大上传至少留一个 Waitress 线程处理 heartbeat，worker 一次只有一个上传；不要为上传长时间持 SQLite 锁。

服务端鉴权前限速：Nginx 对 `/sessions` 使用独立 zone（http 级定义），每 IP 每分钟 6 次/burst 3；普通 worker RPC 采用认证后的 session 频率上限 120/min，heartbeat 不受 failed-login 独立桶影响。超限返回 429/Retry-After，不无限增长每 IP 内存映射。说明所有共享出口用户可能受 IP 桶影响，不以其替代认证。

- [ ] **Step 4: GREEN and syntax verification.** 运行 Step 1；`python -m compileall -q src`；Linux 测试环境执行 `systemd-analyze verify` 对两个 unit，完整 nginx 测试配置执行 `nginx -t`；本地无工具则标待 Linux 验证，不能写已通过。
- [ ] **Step 5: Commit.** 逐一 git add 本任务新文件及 pyproject，`git commit -m "feat: package remote controller and worker services"`；不启动服务。

## Task 10: End-To-End Fault Matrix And Release Gate

**Files:** Create `tests/remote/test_end_to_end.py`, `tests/remote/test_recovery.py`; complete `docs/REMOTE_EXECUTION.md`; update approved spec/plan execution status only after evidence。

**Interfaces:** test-only `Scenario(tmp_path,monkeypatch)` 持有真实临时控制 DB/Flask test client/worker、fake provider、fake clock；`enqueue(kind,novel_id=None)`, `step(seconds=15)`, `run_until(predicate,max_steps=100)`, `restart_vps()`, `restart_worker()`, `drop_next_response(operation)`, `crash_at(point)`。测试 fixture 在本文件完整定义，复用生产协议而不是直接把 run 状态改成功。HTTP transport 适配 test client，不打开真实 socket；clock step 不 sleep。

- [ ] **Step 1: Add full-flow assertions before completing integration.**

```python
def test_synthetic_epub_round_trip_and_idle_suspend(scenario):
    s = scenario
    run = s.enqueue("epub.single.v1", novel_id=42)
    s.run_until(lambda: s.run(run)["state"] == "succeeded")
    result = s.download(run)
    assert result.status_code == 200
    assert result.mimetype == "application/epub+zip"
    assert s.library_unchanged()
    s.step(seconds=301)
    s.run_until(lambda: s.provider.state == "suspended")
    assert s.controller_owned_activation()


def test_published_response_loss_does_not_rerender(scenario):
    s = scenario
    s.drop_next_response("publish")
    run = s.enqueue("diagnostic.v1")
    s.run_until(lambda: s.run(run)["state"] == "succeeded")
    s.run_until(lambda: s.worker_idle())
    assert s.render_count(run) == 1
    assert s.published_count(run) == 1
```

`Scenario` 方法 `run/download/library_unchanged/controller_owned_activation/worker_idle/render_count/published_count` 由 fixture 实现为真实库查询和 test client 请求；不可在测试中硬编码返回 True。其余故障使用下表逐项独立测试，便于明确拒绝某个交付。

- [ ] **Step 2: RED then integrate missing wiring only.** `python -m pytest tests/remote/test_end_to_end.py tests/remote/test_recovery.py -q`；修复只落对应责任模块，不绕过 auth/租约换绿。每次修改重新跑对应 Task 的单元测试。

| 故障/验收 | 必须断言 |
|---|---|
| 实例停机与冷却 | 阅读、登录、搜索、旧导出仍成功；远程只显示等待 |
| start 202、operation 未完成 | 不派任务、不显示成功 |
| 两次同 key / 小说已更新 | 原 run/快照不变；不同 request 409 |
| 并发 claim、丢领取响应 | 一个 active attempt，原 claim receipt 可重取 |
| 120 秒本地停止、180 秒租约失效 | 旧 worker 不再工作，过期 heartbeat 不复活，重试最多 3 次 |
| 上传中取消/过期 | 暂存不能发布，旧结果不污染新 attempt |
| 文件写前/写后 DB 前/DB 后回执前崩溃 | 无损坏引用、无双发布、孤儿可安全 GC |
| VPS 重启、worker 冷启动 | epoch 正常保留；新 boot 退役旧授权；不受主 task_logs 清理影响 |
| 旧备份恢复 | API 关闭恢复后换 epoch/凭据；旧授权失败；ownership 不误认为仍受控 |
| draining 新需求、人工 hold、延迟 suspend | 队列不丢，操作串行收敛，不提前派发，不误暂停已知人工会话 |
| 恶意 ZIP/路径/URL/超限/未知 task | 拒绝，无 shell/网络/任意文件写，无秘密日志 |
| 两种认证互换、缺 CSRF、匿名下载 | 拒绝，固定 domain 下跨设备合法用户可读自己的私有书库任务 |

- [ ] **Step 3: GREEN and full verification.**

```powershell
python -m pytest tests/remote -q
python -m pytest -q
python -m compileall -q src
python scripts/build_tailwind_css.py --check
git diff --check
```

记录实际结果、失败和平台限制，不预填通过数量。追加 390px/1280px 页面截图验收和 Linux service/nginx 验证结果。默认测试全程不要求真实账户 key。

- [ ] **Step 4: Release checklist and staged activation.** 在 `docs/REMOTE_EXECUTION.md` 写清：默认关闭 -> fake 测试 -> 获用户授权后核对生产版本/备份/目标机器/网络 -> 只跑合成诊断 -> 合成 EPUB -> 用户选一本真实小说。不得从计划自动跳到生产。真实 202/冷启动/暂停/恢复、私有输入传输以及可恢复备份都分别记录；没有证据的保持未验证。
- [ ] **Step 5: Rollback and commit.** 回退先停止新提交/claim、撤销 attempt、处理未确认结果，再停 controller；它不能在退出时擅自停云电脑。保留控制 DB/输入和结果以便诊断；关闭 feature 不删除主库、归档或已发布产物。提交测试/手册，`git commit -m "test: verify interrupted remote execution lifecycle"`。不自动 push/merge/deploy。

## Spec Coverage And Deliberate Deferrals

| 已批准约束 | 本计划覆盖 |
|---|---|
| VPS 唯一业务数据源、阅读独立云状态 | Tasks 2–4、10；worker 不读业务 DB |
| durable event、attempt、lease、fencing | Task 2；普通重启与旧备份恢复区别在 Task 9 |
| ready 不等于 running、平台真实状态/冷却 | Tasks 5、6；不以固定 sleep 掩盖 readiness |
| 安全上传、原子发布、结果未知 | Tasks 3、4、7、10 |
| 不误暂停人工实例、draining 竞态 | Task 6；人工 hold 在 Task 8 |
| 试点入口、兼容旧导出、无秘密下发 | Tasks 1、3、4、7、8 |
| 冷启动、最小权限、容量、备份回退 | Tasks 7、9、10 |
| 签到/抓取/阅读进度后续独立规格 | 明确不纳入一期，不注册其 task，不宣传已经完成 |

方案中的统一业务 cron、第三方副作用核实、Pixiv 抓取凭据与分批提交、跨设备阅读进度是后续阶段。本期手动提交验证底座，不假装已迁移定时任务，也不创建 app 定时自动化。

公开 provider 参考（本次查询未使用账户凭据）：

- [Get machine](https://instabox.sh/docs/api-reference/machines/get-a-sandbox.md)
- [Start](https://instabox.sh/docs/api-reference/machines/start-a-stopped-or-paused-sandbox.md)
- [Resume](https://instabox.sh/docs/api-reference/machines/resume-a-paused-sandbox-where-it-left-off.md)
- [Suspend](https://instabox.sh/docs/api-reference/machines/pause-a-running-sandbox-keeping-its-running-programs.md)
- [Operation](https://instabox.sh/docs/api-reference/operations/get-an-operation-the-progress-of-a-sandbox-change.md)
- [Wake](https://instabox.sh/docs/api-reference/machines/resume-an-idle-suspended-machine-before-using-it.md)
- [Limits and max-run](https://instabox.sh/docs/machines/auto-pause.md)

## Execution Handoff

推荐在同一聊天的隔离 worktree 中使用 subagent-driven-development：一个任务由实现者执行，经过规格与代码质量审查后推进；Task 5 可与早期队列/存储任务并行，其余按依赖收口。另一个选项是 executing-plans 在当前聊天分批串行执行并在检查点回报。两者都不包含未经确认的线上启用。
