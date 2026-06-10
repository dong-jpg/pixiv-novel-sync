# Phase 7.6: 长任务改后台job

## 背景
preferences.analyze_local和recommendations.run是长时间运行的任务:
- analyze_local: 遍历全部小说统计(数千条记录)
- recommendations.run: 多查询+翻页+API延迟(分钟级)

当前同步调用会阻塞HTTP请求,超时风险高。

## 改造计划
1. JobType新增PREFERENCE_ANALYZE和RECOMMENDATION_RUN
2. jobs/tasks/新增对应task实现
3. webapp.py端点改为启动job并返回job_id
4. 前端轮询/api/jobs/{job_id}获取进度和结果

## 优先级
P2 - 非关键路径,但影响大数据集体验

## 预估工作量
2-3天(含测试)
