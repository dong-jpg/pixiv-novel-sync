# ✅ 项目审计 - 行动检查清单

快速参考版本 - 打印此清单跟踪进度

---

## 🔴 Week 1: Critical Bug 修复

### Day 1-2: 并发安全
- [ ] **Bug #1**: 信号量泄漏 → 添加 finally 块
  - `web/managers.py:314`
  - `web/managers.py:736`
  - `jobs/quick_sync.py:134`
- [ ] **Bug #2**: SQLite 竞态 → 所有操作加锁
  - `ai/retrieval.py:61` (TFIDFRetriever)
  - `ai/retrieval.py:233` (EmbeddingRetriever)
  - `ai/retrieval.py:387` (APIEmbeddingRetriever)
- [ ] **Bug #3**: 外键约束 → 添加 PRAGMA
  - `storage/connection.py:19`

### Day 3: 数据一致性
- [ ] **Bug #4**: 锁外读取 → 移入锁保护
  - `web/managers.py:219`
- [ ] **Bug #5**: 删除顺序 → 调整顺序
  - `storage/users.py:259`
- [ ] **Bug #6**: FTS 事务 → 包裹事务
  - `storage/novels.py:371`

### Day 4-5: 验证与改进
- [ ] 编写并发测试用例
- [ ] 压力测试（1000 并发请求）
- [ ] 统一 API 错误响应格式
- [ ] 添加 Toast 提示组件

---

## 🟠 Week 2: 文档与用户体验

### Day 1-2: 文档更新
- [ ] 更新 README.md
  - [ ] 添加 AI 创作工作台章节
  - [ ] 添加智能推荐系统章节
  - [ ] 更新项目结构
  - [ ] 更新功能列表
  - [ ] 添加 Logo
- [ ] 完善 API 文档
  - [ ] 使用 OpenAPI 3.0 规范
  - [ ] 记录所有 71 个端点
  - [ ] 添加请求/响应示例

### Day 3: 移动端优化
- [ ] 底部浮动操作栏
- [ ] 汉堡菜单
- [ ] 触摸手势优化
- [ ] 响应式 CSS 完善

### Day 4: 错误体验改进
- [ ] 简化错误消息
- [ ] 添加操作建议
- [ ] 错误分级（INFO/WARN/ERROR/FATAL）
- [ ] 相对时间显示

### Day 5: 首次配置向导
- [ ] 检测未配置状态
- [ ] 多步骤 UI 流程
- [ ] Token 获取集成
- [ ] 配置验证

---

## 🟡 Week 3-4: 功能完善

### Week 3: AI 推荐增强
- [ ] 实现 AI 偏好总结（Phase B）
  - [ ] `stream_analyze_preferences` 方法
  - [ ] Prompt 模板设计
  - [ ] 前端流式展示
- [ ] 实现 AI 创作偏好注入（Phase F）
  - [ ] 添加 preference_profile_id 参数
  - [ ] Prompt 中注入偏好
  - [ ] 强度控制（关闭/轻/中/强）

### Week 4: 性能与测试
- [ ] 批量事务优化
  - [ ] 实现 50 条/批次
  - [ ] 失败回滚机制
  - [ ] 性能基准测试
- [ ] 测试覆盖率提升
  - [ ] sync_engine 单元测试
  - [ ] storage 集成测试
  - [ ] AI 端到端测试
  - [ ] Web API 测试

---

## 📊 质量检查点

### 代码质量
- [ ] 所有 critical bugs 已修复
- [ ] pylint 评分 >8.0
- [ ] 测试覆盖率 >70%
- [ ] 无 TODO/FIXME 标记

### 文档质量
- [ ] README 准确度 >90%
- [ ] API 文档覆盖率 100%
- [ ] 所有公开 API 有示例
- [ ] 常见问题已记录

### 用户体验
- [ ] 首次配置 <5 分钟
- [ ] 核心操作 <3 次点击
- [ ] 移动端流畅使用
- [ ] 错误消息可理解

### 性能指标
- [ ] 同步速度 >100 本/分钟
- [ ] 全文搜索 <100ms
- [ ] API 响应 p95 <500ms
- [ ] 7×24 稳定运行

---

## 🎯 里程碑

### Milestone 1: 稳定性 (Week 1 结束)
- [x] 所有 critical bugs 修复
- [x] 并发测试通过
- [x] 无死锁风险

### Milestone 2: 可用性 (Week 2 结束)
- [x] 文档更新完成
- [x] 移动端体验优化
- [x] 新用户引导完善

### Milestone 3: 功能完整 (Week 4 结束)
- [x] AI 推荐系统 95% 完成
- [x] 性能提升 30%+
- [x] 测试覆盖率 >70%

---

## 📈 进度追踪

**当前状态**: [ ] 未开始 / [ ] 进行中 / [ ] 已完成

**本周完成**: ___ / ___ 任务  
**总体进度**: ____%

**遇到的问题**:
1. 
2. 
3. 

**下周计划**:
1. 
2. 
3. 

---

## 🔄 每日站会模板

### Yesterday
- 完成了什么？
- 遇到什么问题？

### Today
- 计划做什么？
- 需要什么帮助？

### Blockers
- 有什么阻碍吗？

---

## 📞 需要帮助？

如果卡住了，参考这些文档：
- 📋 **EXECUTIVE_SUMMARY.md** - 执行摘要
- 🐛 **CRITICAL_BUGS_FIX_PLAN.md** - Bug 修复详细指南
- 🚀 **OPTIMIZATION_ROADMAP.md** - 完整优化路线图
- 📊 **AUDIT_REPORT.md** - 90+ 页完整报告

或者随时问我！

---

**创建日期**: 2026-06-16  
**最后更新**: ___________  
**负责人**: ___________
