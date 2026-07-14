# 发布阻断问题修复实施计划

> **供代理执行：** 必须使用 `subagent-driven-development` 或 `executing-plans` 逐项执行。所有代码修复采用测试驱动开发（TDD），步骤使用复选框跟踪。

**目标：** 修复当前两个待推送提交中的确定性回归和审计确认的安全缺口，在不扩大重构范围的前提下达到可验证、可推送状态。

**架构：** 先隔离测试运行环境，再依次修复 Web 信任边界、AI 数据事务、Provider 出站传输和 `.env` 写入，最后恢复 UI 导航并纠正文档。每个任务独立提交、独立审查，最终在临时数据库和存储目录下运行完整验证。

**技术栈：** Python 3.10+、Flask、SQLite、Requests、urllib3、Pytest、Vue 模板、Git、PowerShell、Bash。

## 全局约束

- 不删除、覆盖或提交原工作区中的 `.env`、数据库、`.claude/`、`memory/` 和其他用户未跟踪文件。
- 不连接真实 Pixiv 或 AI 服务；测试中的 DNS、HTTP 和存储全部隔离。
- 每项生产代码修复必须先看到对应测试因目标缺陷失败。
- 不拆分大型模板，不重写完整 API 手册，不重建知识图谱，不重新设计部署体系。
- 除专有名词、代码标识和协议缩写外，新增文档与测试说明使用中文。

---

### 任务 1：隔离测试数据库、文件目录和 DNS

**文件：**
- 新建：`tests/conftest.py`
- 新建：`tests/test_test_isolation.py`
- 修改：`tests/test_ai_providers_fallback.py`

**接口：**
- 产出：自动启用的 `isolate_runtime_paths` Pytest 夹具。
- 产出：Provider 回退测试专用的固定公网 DNS 夹具。

- [ ] **步骤 1：写测试证明运行路径必须位于 `tmp_path`**

```python
def test_runtime_paths_are_isolated_from_repository(tmp_path):
    for name in ("PIXIV_DB_PATH", "PIXIV_PUBLIC_DIR", "PIXIV_PRIVATE_DIR"):
        configured = Path(os.environ[name]).resolve()
        assert configured.is_relative_to(tmp_path.resolve())
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m pytest tests/test_test_isolation.py -q -p no:cacheprovider`

预期：因环境变量不存在或仍指向仓库路径而失败。

- [ ] **步骤 3：增加自动隔离夹具**

```python
@pytest.fixture(autouse=True)
def isolate_runtime_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "test.db"))
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(tmp_path / "private"))
```

在 `test_ai_providers_fallback.py` 增加自动夹具，将 `pixiv_novel_sync.ai.providers.socket.getaddrinfo` 固定为一个公网 IPv4 结果，避免测试访问实时 DNS。

- [ ] **步骤 4：验证隔离且不改真实数据库**

运行：`python -m pytest tests/test_test_isolation.py tests/test_ai_providers_fallback.py tests/test_preference_jobs.py -q -p no:cacheprovider`

同时比较原工作区 `data/state/pixiv_sync.db` 的运行前后 SHA-256，预期完全一致。

- [ ] **步骤 5：提交**

提交信息：`test: isolate runtime paths and provider DNS`

---

### 任务 2：关闭代理认证绕过并恢复向导导航

**文件：**
- 修改：`tests/test_webapp_security.py`
- 修改：`tests/test_frontend_library_os.py`
- 修改：`src/pixiv_novel_sync/webapp.py`
- 修改：`src/pixiv_novel_sync/templates/dashboard_ai.html`

**接口：**
- 保留：可信代理模式下 `_client_addr()` 按右数第 N 跳取真实客户端。
- 删除：未信任代理模式下 `_is_local_proxy_request()` 的免认证旁路。
- 恢复：`pageMode === 'wizard'` 时的 `wizard`/`distill` 分段导航。

- [ ] **步骤 1：增加两个失败回归测试**

认证测试使用 `DASHBOARD_TRUST_PROXY=false`、无 `DASHBOARD_TOKEN`、`REMOTE_ADDR=127.0.0.1`、`X-Forwarded-For=127.0.0.1, 203.0.113.10`，断言 `/dashboard` 返回 403；仅含本机 `X-Real-IP` 时同样返回 403。

模板测试读取 `dashboard_ai.html`，断言存在 `v-if="pageMode === 'wizard'"` 的导航容器、`v-for="tab in tabs"` 和 `switchTab(tab.id)`。

- [ ] **步骤 2：运行并确认失败原因正确**

运行：`python -m pytest tests/test_webapp_security.py tests/test_frontend_library_os.py -q -p no:cacheprovider`

预期：旧的本机代理测试仍返回 200，且模板没有 wizard 模式导航。

- [ ] **步骤 3：实施最小修复**

删除 `_is_local_proxy_request()` 及其调用。未配置 token 且检测到代理头、同时未启用可信代理时，直接返回 403。可信代理分支保持现有右数跳数逻辑。

在 AI 项目专用顶栏之前增加 wizard 模式导航，复用现有 `tabs`、`activeTab` 和 `switchTab()`；AI 项目模式顶栏和项目子 Tab 不回退。

- [ ] **步骤 4：验证聚焦测试**

运行：`python -m pytest tests/test_webapp_security.py tests/test_frontend_library_os.py tests/test_html_cache_headers.py -q -p no:cacheprovider`

- [ ] **步骤 5：提交**

提交信息：`fix: close proxy auth bypass and restore wizard navigation`

---

### 任务 3：让 AI 状态与向导导入原子化

**文件：**
- 新建：`tests/test_ai_import_atomicity.py`
- 修改：`src/pixiv_novel_sync/ai/services/projects.py`
- 修改：`src/pixiv_novel_sync/ai/services/chat_wizard.py`

**接口：**
- `_parse_and_save_state()`：单次调用最多新增 200 条伏笔，重复分段不能重置计数，失败时完全回滚。
- `_import_wizard_payload()`：先完整规范化输入，再用一个数据库事务写入。

- [ ] **步骤 1：写状态解析失败测试**

使用真实临时 SQLite 数据库创建项目，向 `_parse_and_save_state()` 传入包含 `character_state`、`plot_progress` 和 `new_foreshadows` 的输出。当前代码应因 `_MAX_STATE_FORESHADOWS` 未定义而失败。

再增加重复 `new_foreshadows` 分段总计超过 200 条的测试，以及模拟伏笔写入异常后状态记录不存在的回滚测试。

- [ ] **步骤 2：写向导导入失败测试**

创建真实会话，传入第二章为非字典或非法章节号的内容，断言抛出 `AIServiceError`，并且项目数、章节数、伏笔数和会话状态均未变化。另测两个超过 2000 字且裁剪后相同的伏笔只写入一次。

- [ ] **步骤 3：运行并确认失败**

运行：`python -m pytest tests/test_ai_import_atomicity.py -q -p no:cacheprovider`

- [ ] **步骤 4：实施状态事务修复**

将 `added_foreshadows = 0` 放在分段循环外，统一使用 `_MAX_STATE_NEW_FORESHADOWS`，并用：

```python
with db.transaction():
    # 解析并写入全部状态和伏笔
```

包裹整个写入过程。

- [ ] **步骤 5：实施向导导入规范化与事务**

新增私有规范化函数，要求 project 为字典、chapters/foreshadows 为列表、每一项为字典、章节号为 `1..2147483647`。字符串先裁剪后去重，`settings` 必须为字典。完成全部规范化后再进入 `db.transaction()` 执行项目、章节、伏笔和会话写入。

- [ ] **步骤 6：验证聚焦测试**

运行：`python -m pytest tests/test_ai_import_atomicity.py tests/test_ai_service_parsing.py tests/test_storage_db.py -q -p no:cacheprovider`

- [ ] **步骤 7：提交**

提交信息：`fix: make AI state and wizard imports atomic`

---

### 任务 4：固定 Provider 已校验 IP 并禁止重定向

**文件：**
- 修改：`pyproject.toml`
- 修改：`tests/test_ai_security_hardening.py`
- 修改：`tests/test_ai_providers_fallback.py`
- 修改：`src/pixiv_novel_sync/ai/providers.py`

**接口：**
- 新增 `_ResolvedTarget`：保存规范 URL、原始主机名、端口、Host 请求头和已校验 IP。
- 新增 `_PinnedHostAdapter(HTTPAdapter)`：连接池 host 使用已校验 IP，HTTPS 的 `assert_hostname` 与 `server_hostname` 使用原始主机名。
- 新增 `AIProvider._post()`：每次请求重新解析并固定目标，设置 `allow_redirects=False`，拒绝 3xx。

- [ ] **步骤 1：增加地址分类测试**

测试默认拒绝 `100.64.0.1`，允许公网 IPv4-mapped IPv6，私有地址开关仍不能放行链路本地和共享地址。

- [ ] **步骤 2：增加连接固定测试**

构造 `_PinnedHostAdapter(hostname="api.example.com", ip="93.184.216.34")` 和 PreparedRequest，断言连接池参数中的 host 为固定 IP，`assert_hostname`、`server_hostname` 仍为原始域名，Host 请求头不变。

增加本地 HTTP 服务器测试：DNS 模拟仅第一次将 `rebind.test` 解析到允许的回环地址，实际请求必须直接连接该 IP，且服务器看到 `Host: rebind.test:<port>`；测试环境显式启用私有地址开关。

- [ ] **步骤 3：增加重定向测试**

模拟 302 和 307 响应，断言 `AIProviderError`，并断言 `Session.post` 收到 `allow_redirects=False`，不会产生第二次请求。

- [ ] **步骤 4：运行并确认失败**

运行：`python -m pytest tests/test_ai_security_hardening.py tests/test_ai_providers_fallback.py -q -p no:cacheprovider`

- [ ] **步骤 5：实施解析和固定适配器**

先展开 IPv4-mapped IPv6，再执行地址分类。默认规则为非 `is_global` 即拒绝；私有地址开关只放行 private/loopback。

`_PinnedHostAdapter.build_connection_pool_key_attributes()` 调用父类后覆盖：

```python
host_params["host"] = self._ip
pool_kwargs["assert_hostname"] = self._hostname
pool_kwargs["server_hostname"] = self._hostname
```

所有 OpenAI-compatible 和 Anthropic 流式、非流式请求改走 `_post()`。在依赖中直接声明 `urllib3>=2.0,<3`，保证连接池参数可用。

- [ ] **步骤 6：验证聚焦测试**

运行：`python -m pytest tests/test_ai_security_hardening.py tests/test_ai_providers_fallback.py tests/test_ai_service_provider_cache.py -q -p no:cacheprovider`

- [ ] **步骤 7：提交**

提交信息：`security: pin validated provider destinations`

---

### 任务 5：统一 `.env` 安全原子写入

**文件：**
- 新建：`src/pixiv_novel_sync/utils_env.py`
- 新建：`tests/test_env_security.py`
- 修改：`src/pixiv_novel_sync/oauth_helper.py`
- 修改：`src/pixiv_novel_sync/webapp.py`
- 修改：`src/pixiv_novel_sync/sync_engine.py`

**接口：**
- 新增 `secure_atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None`。

- [ ] **步骤 1：增加失败测试**

在 POSIX 环境把已有 `.env` 设为 `0600`，分别执行 OAuth token 保存、Flask secret 初始化和 Web Cookie 保存，断言最终权限仍为 `0600`。另用预建符号链接验证临时文件不会被跟随；Windows 跳过仅与 POSIX 权限有关的断言。

- [ ] **步骤 2：运行并确认失败**

运行：`python -m pytest tests/test_env_security.py tests/test_oauth_helper.py tests/test_webapp_security.py::test_flask_secret_fallback_persists_to_env -q -p no:cacheprovider`

- [ ] **步骤 3：实施共用写入器**

使用随机临时文件名和 `os.O_CREAT | os.O_EXCL`，可用时增加 `os.O_NOFOLLOW`。循环调用 `os.write` 直到 payload 全部写完，随后 `os.fsync`、关闭、`os.replace` 和最终 `os.chmod`；异常时只清理本次创建的临时文件。

- [ ] **步骤 4：替换三个调用入口**

OAuth、Flask secret 和 Web Cookie 入口只负责组装最终字节内容，全部调用 `secure_atomic_write()`，不保留各自的临时文件实现。

- [ ] **步骤 5：验证聚焦测试**

运行：`python -m pytest tests/test_env_security.py tests/test_oauth_helper.py tests/test_webapp_security.py tests/test_sync_engine_incremental.py -q -p no:cacheprovider`

- [ ] **步骤 6：提交**

提交信息：`security: centralize private env writes`

---

### 任务 6：纠正文档、版本和仓库卫生

**文件：**
- 新建：`LICENSE`
- 新建：`.gitattributes`
- 修改：`.gitignore`
- 修改：`README.md`
- 修改：`docs/INDEX.md`
- 修改：`docs/API_COMPLETE.md`
- 修改：`docs/AI_WRITING_STUDIO_PLAN.md`
- 修改：`KNOWLEDGE_GRAPH.md`
- 修改：`deploy.sh`
- 修改：`scripts/install_server.sh`
- 修改：`src/pixiv_novel_sync/webapp.py`
- 修改：`tests/test_webapp_security.py`

**接口：**
- 健康接口版本来自 `pixiv_novel_sync.__version__`，不再硬编码。
- README 的操作命令必须能映射到当前 CLI 或现有路由。
- 根目录 `deploy.sh` 是唯一推荐的 Web 部署入口；旧 timer installer 明确标为历史/高级用途。

- [ ] **步骤 1：增加版本失败测试**

请求 `/api/health`，断言 JSON `version == pixiv_novel_sync.__version__`。当前硬编码 `1.0.0` 应失败。

- [ ] **步骤 2：修正运行时版本**

在 `webapp.py` 导入 `__version__` 并用于健康接口，保持 `pyproject.toml` 和包版本的 `0.1.0` 一致。

- [ ] **步骤 3：修正文档真值**

README 用 CLI 示例 `pixiv-novel-sync sync bookmark following_novels subscribed_series` 替换失效 curl；AI 部分指向 `/dashboard/ai` 和 `frontend-api-contract.md`；配置块改为引用 `.env.example` 与 `config/config.yaml.example`；修正数据目录；删除未声明工具的可直接运行承诺或给出额外安装说明。

为 `API_COMPLETE.md`、`AI_WRITING_STUDIO_PLAN.md`、`KNOWLEDGE_GRAPH.md` 添加“历史快照，不是当前事实来源”提示，并在 `docs/INDEX.md` 中移入历史参考区域。

- [ ] **步骤 4：补齐许可证和仓库规则**

增加标准 MIT 许可证，版权行为 `Copyright (c) 2026 dong-jpg`。`.gitignore` 增加 `/.claude/`、`/memory/`、`config/config.yaml.bak`；`.gitattributes` 为 `*.sh`、`*.service`、`*.timer`、`*.conf` 固定 LF。

- [ ] **步骤 5：收敛部署说明**

将 `deploy.sh` 的 Nginx 对外端口改为 80，增加 `umask 077` 并确保 `.env` 为 `0600`。在 `scripts/install_server.sh` 顶部明确它只负责旧的 timer 同步部署，Web 部署使用根目录脚本；不在本次重写 systemd 架构。为需要直接执行的脚本设置 Git 可执行位。

- [ ] **步骤 6：验证文档和脚本**

运行 Markdown 本地链接检查、`bash -n deploy.sh update.sh scripts/install_server.sh scripts/clear-cache.sh`、`git diff --check`，并确认所有说明中的路由/命令可在代码中找到。

- [ ] **步骤 7：提交**

提交信息：`docs: align release and deployment guidance`

---

### 任务 7：完整验证与发布复核

**文件：**
- 验证：全部已跟踪源代码、测试、文档和部署文件。

- [ ] **步骤 1：运行静态与语法检查**

运行 Python AST/`compileall`、CLI `--help`、Bash `-n`、`git diff --check` 和 `git fsck --no-dangling --no-reflogs`。

- [ ] **步骤 2：运行完整测试**

为 `PIXIV_DB_PATH`、`PIXIV_PUBLIC_DIR`、`PIXIV_PRIVATE_DIR` 设置全新的系统临时目录，运行：

`python -m pytest -q -p no:cacheprovider`

预期：全部测试通过，真实数据库 SHA-256 不变。

- [ ] **步骤 3：最终代码审查**

以 `d3ed802..HEAD` 生成完整审查包，由独立审查代理检查规格符合性、实现质量、安全边界和测试覆盖。Critical/Important 问题必须修复并复审。

- [ ] **步骤 4：刷新远端并复核 Git 状态**

运行 `git fetch --prune origin`，确认本地不落后远端；检查原工作区未跟踪文件仍未删除或提交。

- [ ] **步骤 5：集成并推送**

只有前述门槛全部通过后，才将 `codex/audit-fixes` 快进合并到 `main`，再次运行最终状态检查，并执行 `git push origin main`。
