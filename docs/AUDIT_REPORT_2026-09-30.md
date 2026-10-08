# 全分支执行计划与代码审查报告（2026-09-30）

> **历史审查快照**：本文记录修复前的证据与测试基线。后续修复、最新测试与剩余环境验收请看 [当前整改状态](REMEDIATION_STATUS_2026-09-30.md)。旧复现命令在修复后可能按预期断言失败。

## 1. 结论

**项目尚未完全开发完成，不建议以“全部完成、无隐藏问题”作为发布结论。**

- main 的同步、归档、偏好、救援和 AI 基础设施已有大量实现，当前全量测试通过；但故障注入和调用链复核仍发现数据恢复、终态传播、推荐容错和画像合并缺陷。
- ai-writing 保留写作/成人模块；T4-02、T4-09 已提交并核对，T4-01 只有未提交的部分修复，其他 T4 与 T5-11 未完成收口。全量测试还有 1 个确定失败。
- “历史计划已归档”“复选框已勾”“某函数存在”“测试全绿”均不能单独证明全部需求完成。没有把历史空勾一律改成已完成。
- 本轮是审查与文档修订：**没有新增业务代码修复、没有修改测试以消除失败、没有提交/推送/合并/删除分支**。接手时已有的 7 个源文件和 3 个测试文件修改全部保留。

本报告替代本日旧草稿。旧草稿中的“未跑测试”“预算 helper 全是死代码”“向导提前写用户消息”“当前仍有简单 cron 回退”已不适用于当前工作区。

## 2. 范围、分支和时间线

审查工作区：`D:/gitcode/pixiv-novel-sync`；当前分支 ai-writing，HEAD `6fa9f9f`，叠加接手时已有的未提交修改。main 使用既有、验证前干净的独立工作树进行测试，未切换当前分支。

| 引用 | SHA | 状态及边界 |
|---|---|---|
| ai-writing | 6fa9f9f | 比 main 多 7 个提交；工作区另有未提交修复，不能当成提交已完成。 |
| main | 364c8b7 | 是 ai-writing 的祖先；没有尚未进入 ai-writing 的独有提交。比 origin/main 多 71 个提交。 |
| worktree-agent-a800fff1 | 3f5860a | 历史起点，被 main/ai-writing 包含，无独立未合并成果。 |
| worktree-agent-a8858b18 | 3f5860a | 同上。 |
| worktree-agent-abe5a4e1 | 3f5860a | 同上；三条旧引用不能被标成最新任务已完成分支。 |
| origin/main | d7425ba | 远端停在 09-11；实时 ls-remote 确认远端只有 main，SHA 与本地跟踪引用一致。 |

完整清单见 [按首次提交时间排序的计划台账](PLAN_AUDIT_2026-09-30.md)：**32 份执行计划，另补查 2 份早期需求/设计**。时间追溯使用 Git 首次加入记录并跟随改名，不以文件名日期冒充创建时间。例如名为 06-06 的 embedding 计划首次提交在 06-08，08-28 的三个 phase 首次提交在 08-31，09-14 计划首次提交在 09-16。

分支缺少写作计划是有意剥离，不算遗漏；旧分支也不能通过修改当前工作区文档就变成完成状态。对于共享祖先的历史分支未重复运行全量测试，远端旧代码未进行部署验收。

## 3. 实测验证

| 检查 | 对象 | 结果 |
|---|---|---|
| python -m pytest -q | ai-writing 当前业务代码/测试工作区 | **1598 passed, 1 failed, 4 skipped**，474.81s |
| python -m pytest -q | main 364c8b7 干净业务代码/测试工作树 | **1253 passed, 4 skipped**，319.13s |
| python -m compileall -q src tests | 两个工作树 | 均 exit 0 |
| python -m pyflakes src | ai-writing | exit 1，8 条提示；其中4条是仍有调用的兼容导出，4条为真实冗余 |
| python -m pyflakes src | main | exit 1，10 条提示，静态检查尚不满足原计划验收 |
| 单独运行失败用例 | test_continue_uses_internal_route_then_main_without_internal_body | **1 failed**，独立复现 |
| 独立审查定向回归 | 非 AI 六个测试文件 | 111 passed（独立审查执行） |
| 文档修改后回归 | test_ai_health、test_ai_model_docs、test_ai_page_routes、test_frontend_library_os、test_scheduler_priority | **113 passed**，22.56s |
| git diff --check / 新审计链接校验 | 当前文档补丁 | 通过 / 无断链 |

唯一全量失败位于 `tests/test_ai_model_router_integration.py:661`：断言期望 internal → main，而当前实际为 5 次 internal → main。新字节分段改变了摘要请求次数；既有测试 fixture 也只准备了两个结果。不能只改断言使其变绿，须先解决下述预算契约和调用成本问题。

本次没有真实调用 Pixiv/Provider、验证 Cloudflare 证书、生产库副本跨分支迁移、生产灰度/48小时观察、全浏览器与移动端交互或大规模性能指标。4 个 skipped 不是通过；编译通过也不代表性能/安全/逻辑已完成验收。

## 4. 优先处理的确认问题

以下行号取当前工作区；确认程度区分“隔离复现”和“确定调用链”，不伪称做过生产环境复现。

### 4.1 P1：异常回滚可能永久丢失归档文件（两活动分支）

- 位置：src/pixiv_novel_sync/webapp.py:241–250。
- 条件：归档移入 .trash 后数据库操作失败，随后恢复文件因权限/占用/I/O 错误再次失败。
- 原因：rollback 捕获恢复异常，仅记录日志，随后清空 _moves 并无条件 rmtree 暂存目录。
- 隔离故障注入结果：original_exists=False，staged_copy_exists=False。数据库记录可能回滚恢复，但文件唯一副本已被删除。
- 处理：保留恢复失败的暂存项及可人工恢复位置，只有成功恢复或明确提交的项才可清理。启动清理也需避免误删待恢复项。这不要求重启已划 OUT 的完整 trash manifest 项目。
- 对应：重新打开 T2-15。建议先处理此项，再进行大批量删除。

### 4.2 P1：token 与 byte 预算契约不一致（ai-writing）

- 位置：ai/model_router.py:748–785；ai/services/core.py:86–124；generation.py:215–226。
- 路由预算由模型 token context window 减去输出 token、开销和安全余量；估算使用 Provider/token heuristic。新 fitter 却直接把 input_budget 当 UTF-8 字节上限。
- 隔离复现：预算1000、固定提示400个汉字，启发式估算266 tokens、实际1200 bytes；fitter 报“Prompt 固定内容超过可用输入预算”。合法输入被拒或上下文被过度裁剪。
- 同输入“前文”重复1000次：HEAD 的 _smart_context 为1次摘要，工作区为5次，增加串行延迟和请求成本；旧单段也未必符合真实窗口，不能简单恢复旧次数。
- 处理：先明确 token/byte 的公共契约，再统一裁剪、摘要分段与测试；既验证不溢出，也验证有效上下文保留。T4-01 仍未完成。

### 4.3 P1：生成自动保存覆盖并发编辑（ai-writing）

- 位置：ai/services/projects.py:1058–1073；storage/ai/writing.py:171–199；dashboard_ai_chapters.html:106。
- 生成沿用开始时 existing_content，每次写入 existing_content + generated；更新没有 expected revision，编辑框也未在生成期间禁用。
- 确定交错路径：开始续写 → 用户在当前或另一标签页保存新正文 → 下一次自动保存以旧正文覆盖新修改。
- 处理：后端 revision CAS/冲突返回，前端明确未保存状态；仅禁用当前页不足以阻止其他客户端。T4-05 未完成。

### 4.4 P1：成人重新生成丢候选且403（ai-writing）

- 位置：dashboard_ai_reader.html:325–335、423–434；ai_web.py:1429–1452。
- 请求前 resetAdultCandidate 清空候选/token/job ID；随后请求未携带 X-Adult-Access-Token，后端要求该 token。
- 处理：保留旧候选和凭证直至新任务成功建立，不绕过安全校验。T4-12 未完成。

### 4.5 P1：Pipeline 断连留下 running（ai-writing）

- 位置：ai/services/projects.py:1805–1822、2153–2171。
- 隔离复现：生成器 next() 取得 metadata 后 close()，Pipeline 仍为 running；终态写入只位于正常循环结束后。
- 处理：整个生成器生命周期须有退出/取消收口，区分已完成、失败、断连。T4-04 未完成。

### 4.6 P2：定时备份截断/失败显示成功（两活动分支）

- 位置：jobs/quick_sync.py:123–176；webapp.py:408–415。
- 聚合只累计数量，丢弃子任务 truncated/incomplete；failed_users 也未被终态判定消费。
- 隔离复现：子任务截断与唯一用户失败两种情况均被判为 succeeded。
- 处理：传播不完整标记、原因和失败用户数，并对部分/全失败制定终态。重新打开 T1-13，T2-20 的轮转成功不代表状态闭环。

### 4.7 P2：候选异常误判全部搜索失败（两活动分支）

- 位置：recommendations.py:151–171。
- 查询异常和候选异常共用 errors，却用 errors == searched 判全部查询失败。
- 隔离复现：一次成功查询、两候选一坏一好，searched=1/candidates=2/saved=1/errors=1，仍抛“推荐搜索全部失败”；正常候选尚未发布，最终丢弃。
- 处理：分开 query_errors/candidate_errors，以真正失败查询数判全失败。重新打开 T1-09。

### 4.8 P2：偏好增量分析覆盖自定义字段（两活动分支）

- 位置：preference_web.py:72–76；jobs/tasks.py:394–404；dashboard_preferences.html:314。
- Web 对缺省 name/description 补默认值，任务层依据“字段存在”判为显式覆盖；前端还硬编码默认名称。
- 隔离请求捕获确认空 JSON 也提交默认名称/说明。
- 处理：保留未提供/显式提供的区别，前后端一起修复。重新打开 T2-47。

### 4.9 其他确定的 AI 问题

| 优先级/任务 | 证据位置 | 触发与影响 |
|---|---|---|
| P2 / T4-03 | dashboard_ai_notes.html:260–270；projects.py:1570–1577 | 自动回收没有 Agent 和 chapter_id，先“Agent 不存在”；补 Agent 后仍“章节内容为空”（服务入口复现）。 |
| P2 / T4-03 | dashboard_ai_chapters.html:887–904；admin.py:1630–1633 | 单步执行固定 agent_id=0，且不处理 SSE error，默认Agent并无回退（服务入口复现）。 |
| P2 / T4-04 | dashboard_ai_chapters.html:866–872、589–623 | Pipeline 完成后先 openChapter 清空步骤，再查失败步骤，自动重试永远看不到失败项。 |
| P2 / T4-07 | projects.py:385–402 | 手写括号配平不识别 JSON 字符串；合法 {"outline":"门上刻着 } 符号"} 被拒绝（隔离复现）。 |
| P2 / T4-08 | adult_prompt.py:181–189、488–489 | 别名和本名共用占位符，原样恢复将“小安”变为“安娜”，非同构（隔离复现）。 |
| P2 / T4-10 | adult.py:2090–2102、2163–2184 | 参与者检查包含目标前后各4000字，上下文旁人被强制列为参与者，拒绝合法目标（规则复现）。 |

这些是已确认问题，不代表其余代码没有缺陷。

## 5. 实现方式评价与无用代码

**合理的方向**：业务/存储 mixin、共享 JobSpec/Runner、事务化推荐发布、变更请求 CSRF、成人失败关闭、模型路由 deadline/取消、文件暂存后数据库操作，都有明确工程价值。不建议为本次问题推倒重写。

**主要设计薄弱点**：跨层契约没有闭环。预算单位、子任务与父任务终态、Web 缺省字段与任务显式覆盖、文件恢复与清理、生成快照与并发写入都在边界处失配。优先补故障注入、端到端契约、并发/CAS测试，而不是只加“源码包含某字符串”的守卫。

| 分类 | 项目 | 判断 |
|---|---|---|
| 真冗余 | settings.py:4 timedelta；storage_files.py:7 time；web/utils.py:136 JobState | 无用导入，可单独清理。 |
| 真冗余 | storage/rescue.py:741 target_exists | 查询结果不参与逻辑，多余局部变量与查询。 |
| 删除入口后的候选死方法 | oauth_helper.py 的 find_task_by_state / sync_state_from_callback_url | ai-writing 当前仓库无调用方；main 仍有旧路由，不能跨分支盲删。 |
| 兼容导出，不能直接删 | webapp 的 _check_novel_status / _check_series_status | jobs/services.py:742、748 仍引用。 |
| 测试兼容导出 | webapp._remove_archive_files | tests/test_archive_integrity.py:7 仍引用，迁移调用后才可删。 |
| 生产 facade 导出 | ai/service.py create_provider | ai/services/core.py:371 经 service_facade 调用，不是死代码。 |
| 有调用的重复逻辑 | _fit_preference_messages 与公共 fitter | 应统一预算契约，但不是死函数。 |
| 有调用的重复校验 | validate_adult_stream_preflight 与 prepare_adult_job | 章节/范围/hash 检查重复；安全校验不可直接放宽。 |
| 审计临时物 | 根目录 audit_routes.py、audit_routes_out.txt | 接手时已存在、未跟踪。本轮不提交也不擅自删除。 |

旧 audit_routes.py 只扫描 .route/.add_url_rule，漏掉大量 .get/.post 等路由，且文本调用计数不能识别动态/外部调用，**不能据此判断路由是死代码**。因此未把“无页面文本匹配”作为自动删除依据。

main 的 pyflakes 还包括 admin.py 未使用 task_type、schema.py 未使用 sqlite3、storage/ai/core.py 无占位符 f-string；也应在对应分支核实/清理。

## 6. 执行计划的真实剩余工作

1. 本轮重新打开 T1-09、T1-13、T2-15、T2-47。另撤回 T3-01/T3-02：工作区删路由/cron 回退不等于指定 main 分支已收口，静态检查也未通过。
2. T4-01：helper 已集中，generation/projects/向导已接线，用户消息改为路由建立后写入；撤回旧报告“尚未接线”的结论。但 token/byte 混用、摘要批次、集成失败仍需处理。
3. T4-02、T4-09 保持已完成；其余 T4 和 T5-11 不勾。T4-06 的生成前 style_control 写回、T4-13 审查实时进度、T4-18 默认年龄18和重审取消等有源码缺口。蒸馏等待已支持取消，不能将该子项继续记为完全未做。
4. T4-11、14、15、16、17 等尚未达到本轮完整验收证据；尤其 _diff_summary 仍为全文 SequenceMatcher(autojunk=False)，但本轮没有可靠证明精确的12000×36000性能阈值，因此记为“未验收”，不编造耗时。
5. 08-14 四份计划按 UNIFIED §1.3 与09-14的替代范围解释；搜索计划表、专用 RECOMMENDATION_SYNC、完整多画像、task-log lease、seen-cursor、完整 trash manifest、4593性能夹具等 OUT 项不是欠缺实现。**现有删除流程的数据丢失缺陷仍必须修复，OUT 不能成为忽略缺陷的理由。**
6. 08-28 phase1–3、09-03 有代码主体与测试；生产性能/灰度/真实 Provider、浏览器交互没有因代码存在而自动验收。Cloudflare 现网证书与生产观察也不得写成已完成。

修订后09-14计划为 **100项已勾、23项未勾**，这只是文档状态，不是项目完成百分比：其中一些任务是多步骤或分支专属，并含未独立验收项。

## 7. 文档更新与交付边界

- 重写本报告，新增32份计划的 Git 时间线与逐分支快照。
- 同步 INDEX、README、CLAUDE、UNIFIED、JOB_SYSTEM、偏好需求、成人指南、前端页面/接口契约：写明已知限制、最新测试、分支与工作区边界。
- 09-14计划撤回6项超前勾选，更新T4-01实况；早期计划与归档入口补状态说明，不篡改历史实现记录、不批量勾完历史步骤。
- 删除五个已不存在端点的“当前可用”契约声明，注明 main 尚未同步；没有把当前工作区端点表套到远端旧版本。
- 文档仅更新当前 ai-writing 工作区；main及旧引用只读检查，没有制造跨分支文档提交。合入主线时应按分支职责迁移相关文档与非写作修复，禁止整支反向合并 ai-writing 到 main。

验证日志保留于本机临时目录：pixiv-audit-20260930-pytest.log、pixiv-audit-20260930-main-pytest.log、pixiv-audit-20260930-pyflakes.log、pixiv-audit-20260930-main-pyflakes.log、pixiv-audit-20260930-repro.log。临时日志可能被系统清理，本报告已保存关键命令、结果与触发条件。

**建议顺序**：保住异常回滚副本 → 修复备份/推荐/画像状态契约 → 统一AI预算并恢复集成测试 → 章节CAS与Pipeline生命周期 → 成人/UI遗留 → 两分支全量、静态检查和真实环境验收后再谈发布完成。

## 8. 两个P1问题的最小复现

在仓库根目录、已安装测试依赖的环境中运行以下 PowerShell 命令。仅操作系统临时目录并核验路径边界，不读取业务数据库、不访问外网。断言用于确认当前缺陷，脚本 exit 0 不表示缺陷已修复。

```powershell
@'
import sys, runpy, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd() / 'src'))
from pixiv_novel_sync.webapp import _ArchiveTrash
from pixiv_novel_sync.ai.services.core import _fit_tail_text_messages, AIServiceError
from pixiv_novel_sync.ai.chunking import estimate_token_count
make_settings = runpy.run_path('tests/test_recommendations.py')['make_settings']
with tempfile.TemporaryDirectory(prefix='pixiv-audit-rollback-') as tmp:
    base=Path(tmp).resolve()
    settings=make_settings(base)
    trash=_ArchiveTrash(settings, [])
    assert trash._trash_root.resolve().is_relative_to(base)
    original=settings.storage.public_dir/'sample'
    assert original.resolve().is_relative_to(base)
    original.mkdir(parents=True)
    (original/'text.txt').write_text('only copy', encoding='utf8')
    trash._move_to_trash(original)
    staged=trash._moves[0][1]
    assert staged.resolve().is_relative_to(base)
    with patch('pixiv_novel_sync.webapp.shutil.move',side_effect=PermissionError('injected restore failure')):
        trash.rollback()
    print('rollback',{'original_exists':original.exists(),'staged_copy_exists':staged.exists()})
    assert not original.exists() and not staged.exists()
fixed='文'*400
print('budget',{'input_budget':1000,'heuristic_tokens':estimate_token_count(fixed),'utf8_bytes':len(fixed.encode('utf8'))})
try:
    _fit_tail_text_messages(lambda s:[{'role':'system','content':fixed},{'role':'user','content':s}], '正文',1000)
except AIServiceError as e:
    print('fitter_rejection',str(e))
else:
    raise AssertionError('expected current defect was not reproduced')
'@ | python -X utf8 -
```

本轮实测输出：original_exists=False、staged_copy_exists=False；400个汉字为266个启发式token/1200字节，输入预算1000仍被fitter拒绝。
