# Work XP Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为用户手动选择的已归档单篇/系列交付全文覆盖、可核验证据的作品级 XP/内容报告，同时严格约束外发、累计预算、恢复、同步让位和敏感产物删除。

**Architecture:** 复用现有 Flask/Jinja/Vue、SQLite、共享 JobManager/JobRunner 和 AIWritingService → ModelRouter → Provider。新增窄的 `work_analysis` 业务包及四张业务表；输入正文只在内存，通用 AI 调用仍 metadata-only。父记录、子调用关联和每次发送前预留组成同一持久化预算链；最终业务发布使用既有 finalization claim，UI 在原偏好页的独立视图展示，不替换个人画像。

**Tech Stack:** Python 3.10+、Flask 3.0.3+、requests、SQLite/WAL、pytest、Jinja/Vue 3.4.38、既有 Tailwind 静态构建器、Node 内置测试及可选离线 Playwright/Chromium；不添加必装第三方框架或服务。

**Approved Spec:** [2026-11-05 作品全文与 XP 分析设计](../specs/2026-11-05-work-xp-analysis-design.md)，用户已确认详细稿；设计提交 `c8956442ba42ca0114bdbd628c790ca1b5a45688`，应用基线 `e8801b09340de3a3630e05065aa80dbf8c1af698`。

**Status:** 实施计划已于 2026-11-09 完成审阅修订；用户随后选择子代理逐任务实施，现进入实施阶段。文内代码、测试及命令仍是实施输入，不是整阶段验收通过声明。实际完成记录见 `.superpowers/sdd/work-analysis-progress.md`；不推送、不部署、不读取用户小说、不发起付费调用。

## Global Constraints

- 一次分析只接受用户明确选择的一篇已归档小说或一个已归档系列。
- 不自动扫描全库、处理新同步作品或执行跨作品批量/定时付费分析；不自动写回个人画像、推荐评分/搜索、收藏/关注或调用 Pixiv 补章。
- 已归档、收藏、追更和“感兴趣”不等于已读且喜欢；报告分析作品，不推断用户身份、性取向或个人喜好。
- 整部优先、仅输入容量超限时分章/块；不能用开头抽样代替全文，不在发送失败后偷偷改变处理粒度。
- 每次调用最多 256 条消息、JSON 不超过 16 MiB；当前归档原文总 UTF-8 不超过 64 MiB、最多 2048 章；原文和汇总单元总数 512；每层最多 16 个子产物，最多 8 层。
- 每父任务请求上限 1–512，计划 token 上限 1–100000000，累计活跃时间上限 30–14400 秒；必须显式填写并确认，不把上限作为无提示默认支出授权。
- 每子 AI job 仍最多 16 个候选、32 次网络请求和 30 分钟；父级预留与子级真实发送序号在同一事务提交后才允许发送。
- 已预留而发送结果不确定的请求保守扣账；已报告用量和本地估算不同，unknown 不补 0，金额/真实计费 token 没有硬保证。
- 正常 pause/cancel/yield 保留受限 epoch 直至一次时间结算；崩溃/撤权保守扣完整未结算时间片；重启不得清零预算或自动重发付费调用。
- 单条引文最多 240 code point，每结论最多 4 条；单元最多 32 个结论，最终最多 128；标签/短标题 80，结论/理由 600，单元概述 2000、最终概述 4000，不确定性/反例各最多 32×600。
- 每个持久化单元结果（包括最终根）最多 128 KiB；服务器完整报告 envelope 最多 1 MiB。超界拒绝/暂停，不静默截断。
- 冻结清单和源 hash，不保存完整输入重放副本；允许有界短证据与极短章重合，必须说明这一留存边界。
- 仅实际正文/hash 改变或删除时失效；同 hash 的时间戳/元数据刷新不使正文报告旧版。新增系列章节不混入旧清单，只标 archive_outdated。
- 所有证据由服务端精确、唯一子串匹配定位；偏移是 Unicode code point 左闭右开区间，不由浏览器 UTF-16 下标推算。
- 首版仅由现有单一 Web 调度 owner 执行，共享单槽、既有线程/JobManager，不增加独立队列/线程池/CLI 执行器；正常同步优先，自动同步关闭不妨碍手动/完成回调驱动的准入。
- 通用调用保持 `metadata_only`，不得使用 `keyword_compat` 保存小说/分析；正文/报告/Key 不进入日志、进度、URL、localStorage 或 rescue/公开 FTS。
- 报告/证据复用 Dashboard 认证、CSRF、文本节点和 `no-store`，默认折叠；删除撤权后清理敏感业务产物，旧 worker 不得复活它们。
- main 的旧 `/ai/jobs/<id>/continue` 仍不支持；新恢复是版本化作品业务恢复，不恢复历史写作模块、个人画像 AI 总结或旧 OUT 工作流。
- 默认测试只用临时数据库、合成正文、fake HTTP/DNS；没有真实 Pixiv/Provider/私有小说调用。真实 1M/计费/网络取消/真机键盘必须另行明确授权。

---

## 1. 工作区、前置门槛与执行方式

当前隔离 worktree：`C:/Users/dong/.codex/worktrees/ai-settings-resume/pixiv-novel-sync`，文档分支 `codex/work-xp-analysis-design`。复用此已附着工作区，不再次创建、删除或归档 worktree。原 `D:/gitcode/pixiv-novel-sync` 的六份文档修改、额外申请文档和 `.probe012/` 不属于本次工作，不能 reset、覆盖或夹带提交。

实施起点是包含本计划的已确认提交，不从远端旧 default branch 或另一份未提交工作树重新开始。执行前记录真实 HEAD/status；若 main 或工作树有未预期变化，先停止核对，不自动 merge/rebase/reset。第一阶段已经完成并部署，不能重跑它的 release helper 或迁移来“恢复上下文”。

执行首次基线（本轮计划编写不执行这些应用测试）：

```powershell
git status --short
git rev-parse HEAD
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'E:\Python\python.exe' -m pytest -q -ra
if ($LASTEXITCODE -ne 0) { throw 'Baseline failed; investigate before implementation' }
& 'E:\Python\python.exe' -m compileall -q src
if ($LASTEXITCODE -ne 0) { throw 'Baseline compile failed' }
git diff --check
```

不修改共享 Python 的 editable install，不为测试安装全局依赖或改变 Git/Windows 路径策略。wheel 构建使用已有具备 wheel 的运行时和短临时目录，缺少依赖按缺口报告。测试环境/浏览器被 skip 不能写成成功。

## 2. 计划包与任务边界

以下五个附录包含完整测试夹具、实现代码块、现有文件定位、RED/GREEN 命令与提交路径。它们是本计划的一部分，不能只读取主文的摘要就实施。每项任务的输入是主文全局约束 + 该附录对应任务 + 规格；不要把所有历史 release 报告重新作为未完成任务。

| 执行顺序 | 独立可审查交付 | 代码/测试说明 |
| --- | --- | --- |
| 1A | 严格值类型、预算和 canonical identity | [Domain Task 1A](2026-11-05-work-xp-analysis/domain.md#task-1a-strict-domain-values-and-canonical-identity) |
| 1B | 本地一致清单、正文版本、零正文持久化 | [Domain Task 1B](2026-11-05-work-xp-analysis/domain.md#task-1b-immutable-metadata-manifest-and-local-source-capture) |
| 1C | 无解密/网络的配置快照、完整候选披露 | [Configuration prerequisite](2026-11-05-work-xp-analysis/configuration.md) |
| 2A–2C | 严格业务产物、精确证据、容量规划与覆盖 | [Domain Task 2](2026-11-05-work-xp-analysis/domain.md#task-2a-strict-artifacts-and-server-resolved-evidence) |
| 3A | 四表 additive schema、源失效/删除、幂等创建及安全投影 | [Storage](2026-11-05-work-xp-analysis/storage.md) |
| 3B | owner/time/状态 CAS、父发送 ledger、检查点/发布/恢复 | [Storage](2026-11-05-work-xp-analysis/storage.md) |
| 4 | 父/子 AI job 关联与真实发送/usage 同事务扩展 | [Runtime](2026-11-05-work-xp-analysis/runtime.md) |
| 5 | 实际全文/汇总执行器、原子有效检查点/报告 | [Runtime](2026-11-05-work-xp-analysis/runtime.md) |
| 6 | 共享准入、手动抢占、控制适配器、重启整理 | [Runtime](2026-11-05-work-xp-analysis/runtime.md) |
| 7 | 业务 service、纯预览、认证 API、幂等/CAS | [API/UI](2026-11-05-work-xp-analysis/ui-api.md) |
| 8 | 既有页面入口、请求所有权、证据视图和真实浏览器 | [API/UI](2026-11-05-work-xp-analysis/ui-api.md) |
| 9 | 下文的整体验收、文档与交付状态同步 | 本文 §6–§8 |

Tasks 3A/3B 分开 review/commit；不把仅有新 schema 当作完整持久化交付。其余任务在代码依赖全部落地后再进入，不让两个实施者同时编辑同一 Router、存储方法、webapp 或模板。独立纯代码可准备测试，但生产接线按表中顺序通过审核。

## 3. 文件职责与依赖图

| 新文件/修改边界 | 职责 |
| --- | --- |
| `work_analysis/contracts.py` | 严格预算、不可变 lease/binding、canonical/hash、安全领域错误 |
| `work_analysis/manifest.py` | 一致源清单、语义 identity、hash 校验和 preview fingerprint |
| `work_analysis/configuration.py` | conn-only 配置快照；保存提示只在内存，纯预检闭包与安全全部候选披露 |
| `work_analysis/artifacts.py`、`evidence.py` | 完整有界 schema、精确引文定位和 reducer 的证据闭包 |
| `work_analysis/planning.py`、`coverage.py` | 整部/分块、sealed units、完整 claim 分组、输入覆盖投影 |
| `storage/work_analysis/` | schema/common/runs/queries/control/accounting/checkpoints 的窄 mixin；四张新业务表 |
| `storage/ai/core.py`、`ai/services/core.py`、`ai/model_router.py` | 默认 None 的服务端父绑定、原子子/父发送/usage 接线；不重写一般调用语义 |
| `work_analysis/ai_bridge.py`、`executor.py`、`coordinator.py` | 窄配置/发送守卫、业务图执行和共享任务适配 |
| `jobs/manager.py`、`runner.py`、`models.py`、`web/managers.py`、`webapp.py` | 原 shared slot 内待运行准入、finalization/cancel、单 owner 生命周期和 Web 接线 |
| `work_analysis/service.py`、`work_analysis_web.py` | strict request envelope、零网络 preview、幂等/CAS、无敏感状态泄露 |
| `static/js/work_analysis.js`、两个 work-analysis partial | 现有偏好页独立视图、同意/预算/操作锁、报告与证据 |
| 既有 preference/novel/series 模板与 detail projection | 深链接入口、同 hash 有界高亮；无参数旧阅读/偏好行为不变 |
| Nginx exact-name JS location | 把新 bare `work_analysis.js` 纳入已经验证的 private/no-store policy，不扩大其他上传/静态策略 |

没有新数据库文件、Provider 协议、后台付费 schedule 或公共 rescue 入口。新包是职责划分，不是替换现有 AI 或同步架构。

## 4. 跨任务不可漂移的接口

完整代码定义在附录。以下表用于执行者交接和最后自查：

| 接口 | 定义/生产者 | 消费者 |
| --- | --- | --- |
| `WorkLease(run_id, epoch, owner_token)`、`WorkCallBinding(lease, unit_id, try_no, configuration_fingerprint)` | 1A | 3B/4/5/6 |
| `preview_fingerprint(selection, manifest, plan, configuration, budget)` | 1B，位于 `manifest.py` | 3A/7；同意 flag 不进此 digest，但进入原提交幂等 identity |
| `snapshot_work_configuration(conn, agent_id, output_tokens)` | 1C | 2/3/4/5/7；返回配置、内存 prompt、接受**完整 payload**的闭包 |
| `seal_unit(fields, manifest_fingerprint=None, child_artifacts=None)` | 2B | 2/3B；reducer 传该 unit 的 child 子集，完整 frontier 另行验证 |
| `validate_derived_units(units, child_units, child_artifacts)` | 2B | 3B；跨组检查不丢 claim/child，单组最多 16 子产物 |
| `materialize_unit(manifest, unit, texts, child_artifacts, *, agent_prompt, output_tokens)` | 2B | 3B/5；正文只在内存 block map，不进入 serialized plan |
| `allowed_child_evidence(unit, child_artifacts)`、`validate_unit_output(raw, unit, block_map, child_artifacts)` | 2A | 3B/5；不得重新复制另一套引文准入逻辑 |
| `publish_work_analysis(lease, report)` | 3B | 5；report 参数是规范化最终 Artifact，storage 构造 <=1MiB envelope |
| `claim_work_analysis(run_id, *, now, slice_seconds, shared_job_id=None, shared_log_id=None)` | 3B | 5；实际激活同时绑定最新 shared job、确切日志行和 epoch，孤立无日志测试可省略 |
| `recover_work_analyses(*, now, process_start=False)`、`reconcile_work_analysis_task_logs()` | 3B | 6；sole owner startup 整理后只修复最新已发布父任务对应日志，不重建已清理日志或重放调用 |
| `get_work_analysis_control(run_id)` | 3A | 6；内部窄控制投影，同 owner/epoch 的同步让位 CAS 重试，不公开 owner 数据 |
| `invoke_text` 新增可选 keyword-only `work_binding=None`；`RouteRequest/RouteJobContext.work_binding=None`、`RouteResult.work_error_code=None` | 4 | 5；完整现有参数和插入位置见 Runtime Task 4，旧调用者维持默认语义 |
| `WorkAnalysisCoordinator(manager, submit_shared, run_shared, settings_provider, *, db_factory, scheduler)` | 6 | 7/Web；唯一 app extension `work_analysis_coordinator` |
| `context["work_control_scope"] = coordinator.work_control_scope` | 6 | 5/Web；仅串行化短暂领取和停止分类，不跨 Provider 调用、检查点或整个 worker 生命周期 |

### 4.1 ID、identity 与 JSON

- run/idempotency ID 为小写 hex32；unit/evidence/claim/fingerprint 为 hex64。源 novel/series ID 为严格非 bool 正整数；章 position 从 0 开始，UI 展示才加 1。
- 来源 semantic identity 不含 fetched/observed 时间；包括目标、顺序、出版声明、已知数量与正文 hash/长度。幂等 identity 固定原始规范化请求，不随预算后续增加而变化。
- Unit 必含 `kind,is_final,level,primary_ranges,context_ranges,child_unit_ids,claim_refs,input_fingerprint,unit_id`。`whole` 是 final，`extract` 不是；reducer 只有完整 frontier 的单一根才 final，模型输出不能升级它。
- 原文块 ID 在 materialize 时由 source/hash/range 生成；evidence ID 还绑定 block ID，避免同一原文短句同时出现在 overlap 和主块时相同 ID 的元数据冲突。
- 新 requests ledger 的 `entry_kind='attachment', request_ordinal=0` **只是子 job 持久关联**。真实请求只能 `entry_kind='request', ordinal=1..32`，所有父发送/usage 计数过滤 request kind；不改变已有 AI usage ordinal 规则。
- 父计划 token 预留是实际候选的 `input_tokens + message_overhead + safety_margin + output_reserve`。现有 `input_tokens` 不包含后两项，不能漏扣；也不能跨候选 max/min。

### 4.2 完整候选披露与容量

纯配置闭包返回的 `work_can_segment` 和 `work_targets` 是本功能内部数据，不能加到全局 v2 API 契约。前者从完整冻结候选事实计算，不被 64 条 public excluded 截断误导；后者列出所有可能使用的安全 Provider/精确模型身份，不仅是当前大输入 eligible 行。原文放不下的小模型可能容纳后续 reducer，因此开始前也必须披露。

Task 7 只把 allowlist 过的 safe target 字段输出给 UI，不能返回配置 fingerprint、密文、URL 或 prompt。配置改变仍使父任务失效，而不是扩充已确认目标。预览只检查本地 ciphertext 是否存在，不解密/远端验证；真正调用引发 legacy upgrade 后配置 fingerprint 变化则在任何发送前停下，显式重新预览。

### 4.3 取消/恢复的线性化点

发送预留事务成功是授权一次调用的边界。取消可以发生在预留与物理发送之间；最后仍尽力停止，但不能承诺零物理发送，且不退款。

最终业务提交前调用 JobRunner 的 `claim_finalization()`；所有作品/通用共享取消及调度让位经同一适配器，不能父报告已提交后又对用户宣称取消先胜出。源变化和删除仍由 DB CAS 使发布失败。不要持 JobManager 锁做 SQLite IO；执行者验证相关锁顺序回归而不是扩大锁范围。

内部 `sync_yield` 只对同一 owner/epoch、清单、配置及预算身份的进度版本变化进行有界 CAS 重试；公开控制接口仍严格使用用户提交的 expected_version。取消许可发出到停止意图落库之间，worker 可以结束网络等待，但必须经内部 `work_control_scope` 等待该短控制操作完成后再分类/结算，不能误记为用户取消。该锁不是新的执行槽，不允许在仍有旧 worker 时提前释放共享槽。

## 5. 每任务实施与审查协议

- [ ] 控制器记录任务起始 SHA、允许写入文件和测试命令；实施者读取自己的附录而不是重新猜全体类型。
- [ ] 先运行该任务的失败回归，确认是目标缺口而非缺依赖/路径故障；保留实际 RED 证据。
- [ ] 按代码/接口步骤实现，运行完整 covering tests；不能增加 sleep 或削弱旧断言来掩盖 race。
- [ ] 审查 spec 与代码质量，展示所有 source/config/retention/budget 边界；重要问题统一修复并复审。
- [ ] 仅在通过后提交明确路径，记录 base→head、结果与未关闭问题；不 amend 或混入他人修改。
- [ ] 每项 gate 更新 `.superpowers/sdd/work-analysis-progress.md`，已完成项恢复会话时不重做。

代码片段是针对当前已检查源码的实施指引。若执行时真实源码接口、schema 或测试前提与片段不一致，报告并修正计划/实现，不能按“计划让我这样写”保留已知缺陷。所有验收结果必须来自实际执行；静态 parse 过计划中的 Python/JS 只证明语法，不等于应用可运行。

## 6. Task 9: 整阶段故障与平台验收

**Files:** 新建 `tests/test_work_analysis_acceptance.py`；更新下文列出的既有项目文档。应用代码只有前八项任务已经审查后才进行整阶段验收。

**Consumes:** Tasks 1–8 全部生产接口及 `tests/work_analysis_runtime_helpers.py` 的真实临时 DB/fake Provider fixture。**Produces:** 已运行的覆盖映射、跨平台与离线浏览器证据、准确状态文档，而不是新的一套实现。

- [ ] **9.1 增加跨模块、原文/用量/报告留存隔离的实际回归。**

```python
import json

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from work_analysis_runtime_helpers import make_runtime_environment


def test_full_work_result_does_not_copy_source_or_reports_into_generic_jobs(tmp_path, monkeypatch):
    marker = "PRIVATE_SYNTHETIC_WHOLE_WORK_SENTINEL_4420"
    report_marker = "PRIVATE_SYNTHETIC_REPORT_SENTINEL_9173"
    env = make_runtime_environment(tmp_path, monkeypatch, texts=[marker + " first chapter", "second chapter tail"])
    try:
        original_stream = env.provider.stream_generate

        def report_stream(*args, **kwargs):
            for chunk in original_stream(*args, **kwargs):
                if chunk.type == "delta":
                    result = json.loads(chunk.text)
                    result["summary"] = report_marker
                    chunk.text = json.dumps(result)
                yield chunk

        monkeypatch.setattr(env.provider, "stream_generate", report_stream)
        manager = JobManager()
        job = manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id}))
        executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
        JobRunner(manager, lambda task, context: executor.run(env.run_id, context)).run(job.job_id)
        report = env.db.get_work_analysis_report(env.run_id)
        assert report["coverage"]["complete"] and report["synthesis_complete"]
        assert report["artifact"]["summary"] == report_marker
        assert job.spec.params == {"run_id": env.run_id}
        generic = [dict(row) for row in env.db.conn.execute("SELECT * FROM ai_jobs")]
        attempts = [dict(row) for row in env.db.conn.execute("SELECT * FROM ai_job_model_attempts")]
        assert generic and attempts
        serialized = json.dumps(generic + attempts, ensure_ascii=False)
        assert marker not in serialized and report_marker not in serialized
        assert all(row["output_text"] is None and row["output_json"] is None for row in generic)
        shared = json.dumps({"progress": job.progress, "stats": job.stats,
                             "logs": [entry.message for entry in job.logs]}, ensure_ascii=False)
        assert marker not in shared and report_marker not in shared
        ledger = env.db.conn.execute(
            "SELECT entry_kind,request_ordinal,planned_tokens FROM work_analysis_requests ORDER BY child_job_id,request_ordinal"
        ).fetchall()
        assert any(row["entry_kind"] == "attachment" and row["request_ordinal"] == 0 and row["planned_tokens"] is None for row in ledger)
        actual = [row for row in ledger if row["entry_kind"] == "request"]
        assert report["usage_summary"]["network_request_count"] == len(actual) == len(env.provider.sent_units)
        assert all(row["planned_tokens"] > 0 for row in actual)
    finally:
        env.close()


def test_old_continue_is_not_the_new_business_resume_path():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    controller = (root / "src/pixiv_novel_sync/static/js/work_analysis.js").read_text(encoding="utf-8")
    assert "/ai/jobs/" not in controller
    assert "work-analyses" in controller
```

继续向 Task 6 已创建的 `tests/test_work_analysis_web_admission.py` 追加以下真实 Web 日志回归，复用该文件的 `web_work` fixture、`wait_for` 和 `JobStatus` 导入。不手工模拟日志写入，也不以空日志查询断言隐私通过：

```python
def test_real_web_task_log_excludes_source_claim_and_report_sentinels(web_work, monkeypatch):
    import json

    app, env = web_work
    source_marker = "synthetic opening and ending"
    report_marker = "PRIVATE_SYNTHETIC_WEB_REPORT_6721"
    claim_marker = "PRIVATE_SYNTHETIC_WEB_CLAIM_8543"
    original_stream = env.provider.stream_generate

    def report_stream(messages, *args, **kwargs):
        sent = json.loads(messages[-1]["content"])
        block = next(item for item in sent["blocks"] if source_marker in item["text"])
        for chunk in original_stream(messages, *args, **kwargs):
            if chunk.type == "delta":
                result = json.loads(chunk.text)
                result.update(summary=report_marker, no_findings_reason=None, claims=[{
                    "dimension": "theme", "certainty": "interpretation", "label": "synthetic",
                    "statement": claim_marker,
                    "quotes": [{"block_id": block["block_id"], "quote": source_marker}],
                }])
                chunk.text = json.dumps(result)
            yield chunk

    monkeypatch.setattr(env.provider, "stream_generate", report_stream)
    app.extensions["work_analysis_coordinator"].wake("created")
    wait_for(lambda: env.db.get_work_analysis(env.run_id)["status"] == "succeeded")
    manager = app.config["job_manager"]
    jobs = list(manager._jobs.values())
    assert len(jobs) == 1
    job = jobs[0]
    wait_for(lambda: env.db.conn.execute(
        "SELECT status FROM task_logs WHERE id=? AND job_id=?",
        (job.progress["log_id"], job.job_id),
    ).fetchone()["status"] == "succeeded")
    assert job.status == JobStatus.SUCCEEDED
    report = env.db.get_work_analysis_report(env.run_id)
    artifact = report["artifact"]
    assert artifact["summary"] == report_marker
    assert artifact["claims"][0]["statement"] == claim_marker
    assert next(iter(artifact["evidence"].values()))["quote"] == source_marker
    task_logs = [dict(row) for row in env.db.conn.execute("SELECT * FROM task_logs")]
    generic = [dict(row) for row in env.db.conn.execute("SELECT * FROM ai_jobs")]
    attempts = [dict(row) for row in env.db.conn.execute("SELECT * FROM ai_job_model_attempts")]
    assert task_logs and generic and attempts
    stored = json.dumps(task_logs + generic + attempts, ensure_ascii=False)
    shared = json.dumps({"params": job.spec.params, "progress": job.progress, "stats": job.stats,
                         "logs": [entry.message for entry in job.logs]}, ensure_ascii=False)
    for sentinel in (source_marker, report_marker, claim_marker):
        assert sentinel not in stored and sentinel not in shared
```

这些是额外跨模块回归，不替代已有 main continue、真实 facade、race、budget 和 API 测试。禁止用源码断言冒充业务恢复验证；仅有原文 marker 也不证明生成报告/证据的留存隔离。

- [ ] **9.2 运行全部新增与关键旧回归。**

```powershell
python -m pytest tests/test_work_analysis_contracts.py tests/test_work_analysis_manifest.py tests/test_work_analysis_configuration.py tests/test_work_analysis_evidence.py tests/test_work_analysis_planning.py tests/test_work_analysis_coverage.py tests/test_work_analysis_storage.py tests/test_work_analysis_executor.py tests/test_work_analysis_coordinator.py tests/test_work_analysis_web_admission.py tests/test_work_analysis_service.py tests/test_work_analysis_api.py tests/test_work_analysis_ui.py tests/test_work_analysis_runtime.py tests/test_work_analysis_browser.py tests/test_work_analysis_acceptance.py -q -ra
if ($LASTEXITCODE -ne 0) { throw 'Work-analysis integration failed' }
python -m pytest tests/test_ai_model_router.py tests/test_ai_job_routing_storage.py tests/test_ai_preflight_api.py tests/test_ai_invocation_runtime.py tests/test_jobs_manager.py tests/test_jobs_runner.py tests/test_jobs_tasks.py tests/test_webapp_jobs.py tests/test_preference_jobs.py tests/test_preference_incremental.py tests/test_ai_model_docs.py -q -ra
if ($LASTEXITCODE -ne 0) { throw 'Existing contract regression failed' }
```

实际测试文件名以附录创建列表和当前仓库清单核对；不存在的旧文件不得通过空选择蒙混。下面的真正全量门槛包括全部新增 Task 4/6 原文件增量回归。

- [ ] **9.3 运行同一最终应用提交的 Windows 全量/静态/Node。**

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
python -m pytest -q -ra
if ($LASTEXITCODE -ne 0) { throw 'Windows full suite failed' }
python -m compileall -q src
if ($LASTEXITCODE -ne 0) { throw 'Compilation failed' }
python -m pyflakes src
if ($LASTEXITCODE -ne 0) { throw 'Undefined-name/static analysis failed' }
$nodeFiles = @(Get-ChildItem -LiteralPath tests -Filter '*.cjs' -File | ForEach-Object { $_.FullName })
node --test @nodeFiles
if ($LASTEXITCODE -ne 0) { throw 'Node runtime suite failed' }
node tests/test_reader_actions_runtime.cjs
if ($LASTEXITCODE -ne 0) { throw 'Legacy reader entry point failed' }
git diff --check
```

若 `pyflakes` 不可用，记录缺口，不能把 compileall 当作等同检查。只可在隔离验证环境安装明确需要的测试依赖；不能更改用户共享解释器绑定。所有日志记录具体 commit、Python/Node 版本、skip 分类和真实 exit。

- [ ] **9.4 在原生 Linux 目录验证 canonical Git 字节。**

必须先完成并提交代码；不要用工作区复制代替实际 Git 导出，也不要使用 Windows 默认 autocrlf 的 archive。创建新归档且先核对它的文件/模式/blob：

```powershell
$sha = (git rev-parse HEAD).Trim()
$archive = Join-Path (Get-Location) ('.superpowers/verification/work-analysis-' + $sha.Substring(0,8) + '-canonical.tar')
git -c core.autocrlf=false -c core.eol=lf archive --format=tar -o $archive $sha
if ($LASTEXITCODE -ne 0) { throw 'Canonical archive failed' }
@'
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tarfile

revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
archive = Path(".superpowers/verification/work-analysis-" + revision[:8] + "-canonical.tar")
entries = subprocess.check_output(["git", "ls-tree", "-rz", "--full-tree", revision])
expected = {}
for item in entries.split(b"\0"):
    if not item:
        continue
    metadata, name = item.split(b"\t", 1)
    mode, kind, oid = metadata.split()
    assert kind == b"blob" and mode in (b"100644", b"100755")
    expected[name.decode("utf-8")] = (mode, oid.decode("ascii"))
with tarfile.open(archive) as exported:
    files = {member.name: member for member in exported.getmembers() if member.isfile()}
    assert set(files) == set(expected)
    for name, (mode, oid) in expected.items():
        body = exported.extractfile(files[name]).read()
        assert hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest() == oid, name
        assert bool(files[name].mode & 0o111) == (mode == b"100755"), name
print(json.dumps({"head": revision, "archive": str(archive), "files": len(expected),
                  "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}))
'@ | python -
if ($LASTEXITCODE -ne 0) { throw 'Archive provenance mismatch' }
```

已有 `.superpowers/sdd/verify-linux.sh` 可作为隔离运行器使用，但先读它并核对每个固定路径仍属本验证目录；只允许执行测试 helper，**不运行任何 remote release helper**。Linux venv 缺失时建立独立新测试环境并记录依赖，不进入生产目录。在原生 Linux 目录解包、设置 `PYTHONPATH=src` 后执行 `python -m pytest -q -ra`，记录最终 SHA 和真实 exit。符号链接/POSIX mode、SQLite 版本专属和浏览器缺失的 skip 单独解释。

- [ ] **9.5 用相同 canonical 源码完成离线真实浏览器、CSS/wheel 字节验收。**

Task 8 已提供实际 browser fixture、375/768/1440 用例和离线构建代码，根验收对最终修复后的稳定提交重跑，不把早期通过数冒充最终代码。使用显式 `AI_UI_VUE_JS` 和既有 Chromium，确认不是 skip；比较源代码/模板/JS/CSS/test/Vue 的前后 hash。每个请求必须在合成 fixture allowlist 内，任何真实域名出网立即失败。

CSS 调用已有 `scripts/build_tailwind_css.py --check --cli <已校验缓存CLI>`；全部新增模板/scripts 在同一真实 wheel 中与被验证源字节一致。Windows wheel 和 canonical LF Linux 字节的比较口径分别标注；不因纯换行差异声称篡改，也不能假称两份产物字节相同。

## 7. W01–W17 覆盖与最终审查

| 规格项 | 主要任务 | 执行时必须核对的证据 |
| --- | --- | --- |
| W01 | 1B、6、7、8 | 手动单作品，未扫描全库/个人画像，无自动付费 schedule，旧 no-view 页面回归 |
| W02 | 1B、2、3A、7、8 | 短/空/缺章、相同日期稳定排序、明确章序/unknown 确认、空目标拒绝 |
| W03 | 1C、7、8 | 真实 conn authorizer、禁止 decrypt/network/job-write 的 preview，过期 fingerprint 冲突 |
| W04 | 1C、2、5 | 整部/分块、16MiB 路径、完整候选容量旗标、未来 reducer 目标披露、未改变计划粒度 |
| W05 | 2C、3B、5、8 | 无缺口/重叠一次、摘要不计原文覆盖、原作完整性与 synthesis 分开 |
| W06 | 2A、3B、8 | exact 唯一引文、emoji codepoint、旧 hash 410、reducer 引用闭包、受控正文高亮 |
| W07 | 2A、3B、5 | invalid/partial 不保存 checkpoint，无自动 JSON 修复循环，最终根有限 |
| W08 | 3B、4、9 | 同事务父/子预留与 attachment、不超最后一份预算、重试/fallback/恢复每发送扣账 |
| W09 | 3B、6 | 正常返还一次、失效 owner 不退款、崩溃保守片、显式预算 CAS/history |
| W10 | 3A、3B、4、5、6 | 两连接创建/发送 race、报告/覆盖原子提交、finalization 两种顺序、迟到 epoch、最新激活日志重启修复 |
| W11 | 3B、6、7、8 | pause/cancel/yield 分开、同 owner 让位 CAS 重试及停止分类屏障、auto-sync off、先真实退出再 P1、启动不自动付费重放 |
| W12 | 3A、3B、4、7、8、9 | metadata-only 全路径、业务产物 auth/CSRF/no-store/转义、UI 不落私密 storage |
| W13 | 3A、3B、5、6、7 | 直接/级联删除、同事务 rollback、撤权清理、迟到 usage 不复活正文产物 |
| W14 | 1B、1C、2、3B、5 | 同 run 缓存、源/配置变更 invalidated、同 hash no-op、后加系列章只标非最新 |
| W15 | 8、9 | 三宽度真实浏览器、刷新预览重新确认、未知写入所有权/版本单调性；Node VM 不替代浏览器/真机 |
| W16 | 3A、3B | 四表重复 additive migration、事务 rollback、FK/CHECK、旧大库不重建写作表 |
| W17 | 4、6、8、9 | 既有 Router/选模/留存、共享同步/抢占、旧偏好与推荐、默认 main continue 全量回归 |

- [ ] 交付从本计划实施基线至最终 HEAD 的完整 diff、所有任务报告、真实平台/资产日志与未关闭问题列表给独立整阶段审查。
- [ ] 优先复核父/子请求账本、终结/取消/源删除互斥、单 owner/单槽和未知状态下幂等重试；重要问题修复后重新运行受影响测试，并使最终平台/浏览器身份可追溯。
- [ ] 不因为某子任务或覆盖表“有测试名称”就勾整阶段完成；根控制器记录真正结果后才关闭门槛。

## 8. 文档、提交与明确不自动发布

在 Task 9 完成时更新现有文档的局部边界，不重写历史审计。实际测试数字和部署状态只在有真实证据后写入：

| 文件 | 精确要同步的事实 |
| --- | --- |
| `README.md` | 手动已归档单作品的内容报告；不是个人画像/推荐，也不是完整原作已核实 |
| `docs/UNIFIED_PROJECT_REQUIREMENTS.md` | 仅局部替代旧 OUT 的作品级报告/父预算/检查点；旧 AI 个人总结/写作/推荐计划仍 OUT |
| `docs/frontend-api-contract.md` | 新 work-analyses 路径、strict envelope、幂等/CAS、状态码、auth/CSRF/no-store、ID-only 进度与证据 410 |
| `docs/frontend-pages.md` | 原偏好页独立视图、当前绑定/未知写入所有权、预算/正文外发确认、证据折叠/同 hash 定位 |
| `docs/MODEL_ROUTING_GUIDE.md` | 默认 None 父绑定，真实发送四项估算预留，unknown usage，旧 main continue 不变 |
| `docs/STATUS.md` | 真实执行 HEAD、逐平台结果/skip、外部手工验收、是否实际推送/部署 |
| 本规格与本计划 | 仅依据真实任务审查/验收更新执行状态，不能把计划完成改成实现 DONE |

可直接采用的功能边界文案：

> 作品分析只处理用户明确选择的已归档单篇/系列；开始前确认当前正文范围、可能使用的模型及预算。结果是带源版本和短证据的作品内容报告，不自动代表个人偏好。输入覆盖、原作完整性和模型解释可靠性分开显示。取消/恢复使用作品级检查点与累计预算，不开放旧 main AI job continue。正文/分析业务产物不进入普通调用历史，用户可以删除；备份/WAL 的物理清除不由应用删除承诺。

执行时将以上文案放到对应功能说明段落，并使用实际 API/测试结果填充事实段；不得用本计划的预期结果预填 passed。文档代码链接/相对链接检查、`test_ai_model_docs.py` 与实际新增文档合同测试通过后，单独提交准确的收尾记录。

**本次计划授权不包含：** 启动真实模型分析、外发用户小说、调用 Pixiv、推送、PR 或生产部署。实施完成后，按 finishing-a-development-branch 流程报告验收/工作区，等待用户明确选择交付操作；不得复用第一阶段曾给过的发布授权自动发布第二阶段。

## 9. 计划编写验收记录（不是应用验收）

2026-11-09，在设计基线 `c8956442ba42ca0114bdbd628c790ca1b5a45688` 的隔离 worktree 完成主文与五份附录复核。以下结果只验证文档、内存中的计划片段及既有文档回归，不能用于勾选 Task 1–9 的实现完成状态。

- 主控制器逐项对照规格 §1–§14 与 W01–W17，检查真实源码接口、任务依赖、占位符及跨附录字段；独立复核分别覆盖存储、运行时和 UI/API。
- 对全部 134 个 Python 代码块进行 Python 3.10 语法解析；其中 2 个明确的调用/异常插入片段使用所在语法结构包装检查，不当作独立模块。5 个 JavaScript 块通过 Node 语法检查；22 个本地文档链接及指定锚点解析。
- 直接从最终 Markdown 提取的纯领域/配置测试：46 项通过；存储片段使用现有 Database 与计划 mixin、SQLite `:memory:`：68 项通过。没有把计划模块写入 `src` 或测试目录。
- 从 Markdown 提取页面控制器及测试，在 Node VM 中执行：17 项通过、0 失败、0 skip。运行时协调器另以合成控制对象验证取消信号/CAS/停止分类屏障、11 类不覆盖更强控制或新 owner 的边界、有界重试、公开严格 CAS 和启动顺序；这些不是完整应用集成测试。
- 既有 `python -B -m pytest tests/test_ai_model_docs.py -q -p no:cacheprovider`：5 项通过。UTF-8、代码块闭合、占位符、空白及工作区差异检查与最后保存的文档一并复核。
- 未执行三条依赖文件型 SQLite/WAL 的并发存储测试；未实施真实调用链、未运行新功能全量/跨平台或实际浏览器验收。重启整合、真实 Web 留存和预算耗尽等新增用例仍须实施后运行；Node VM 不能替代 Vue 调度、布局和手机验收。

本轮仅保存文档，不创建计划提交。`src/`、`tests/`、`config/`、`scripts/` 和 `pyproject.toml` 不变，原 checkout 的既有修改保留。进入实施前记录新的实际 HEAD/status，并按 §1、§5 重新运行基线及逐任务 RED/GREEN 门槛。
