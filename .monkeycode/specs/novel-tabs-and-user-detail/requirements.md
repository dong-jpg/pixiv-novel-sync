# Requirements Document

## Introduction

优化小说页面分类标签和增强关注用户页面功能，提供更好的内容浏览和用户管理体验。

## Glossary

- **收藏小说**: 用户在 Pixiv 上收藏（bookmark）的小说
- **追更系列**: 用户关注的作者的系列小说，或用户主动订阅的系列
- **用户状态**: Pixiv 用户账号的健康状态（正常/封号/资源清空）
- **系列章节**: 属于同一系列的各篇小说

## Requirements

### Requirement 1: 小说页面标签调整

**User Story:** AS 用户, I want 将小说页面的分类标签改为"全部、收藏、追更", so that 更好地区分收藏小说和追更系列

#### Acceptance Criteria

1. WHEN 用户访问小说页面, 系统 SHALL 显示三个标签：全部、收藏、追更
2. WHEN 用户点击"收藏"标签, 系统 SHALL 显示用户收藏的小说列表
3. WHEN 用户点击"追更"标签, 系统 SHALL 显示追更的系列小说列表
4. WHEN 用户点击"全部"标签, 系统 SHALL 显示所有小说

### Requirement 2: 追更系列展示

**User Story:** AS 用户, I want 追更页面展示系列标题, so that 可以快速浏览追更的系列

#### Acceptance Criteria

1. WHEN 用户访问追更页面, 系统 SHALL 按系列分组展示小说
2. WHEN 系列有多个章节, 系统 SHALL 显示系列标题、章节数量、最新更新时间
3. WHEN 用户点击系列, 系统 SHALL 跳转到系列详情页展示各章节
4. IF 系列来自关注用户, 系统 SHALL 标注来源用户

### Requirement 3: 系列详情页

**User Story:** AS 用户, I want 查看系列的所有章节, so that 可以按顺序阅读

#### Acceptance Criteria

1. WHEN 用户访问系列详情页, 系统 SHALL 显示系列标题、简介、作者信息
2. WHEN 系列有多个章节, 系统 SHALL 按顺序展示章节列表
3. WHEN 用户点击章节, 系统 SHALL 跳转到该小说的详情页
4. WHEN 系列有封面图, 系统 SHALL 显示系列封面

### Requirement 4: 关注页面用户状态

**User Story:** AS 用户, I want 查看关注用户的 Pixiv 账号状态, so that 了解用户是否正常

#### Acceptance Criteria

1. WHEN 用户访问关注页面, 系统 SHALL 显示每个用户的账号状态标识
2. WHEN 用户状态为"正常", 系统 SHALL 显示绿色标识
3. WHEN 用户状态为"封号", 系统 SHALL 显示红色标识
4. WHEN 用户状态为"资源清空", 系统 SHALL 显示黄色标识
5. WHEN 无法确定用户状态, 系统 SHALL 显示灰色"未知"标识

### Requirement 5: 用户详情页

**User Story:** AS 用户, I want 查看关注用户的详细信息和小说列表, so that 管理该用户的备份内容

#### Acceptance Criteria

1. WHEN 用户点击关注列表中的用户, 系统 SHALL 跳转到用户详情页
2. WHEN 用户详情页加载完成, 系统 SHALL 显示用户头像、名称、账号、状态
3. WHEN 用户详情页加载完成, 系统 SHALL 显示该用户的小说列表
4. WHEN 用户点击小说, 系统 SHALL 跳转到小说详情页
5. WHEN 用户点击"备份全部"按钮, 系统 SHALL 触发该用户的小说同步

### Requirement 6: 用户小说同步

**User Story:** AS 用户, I want 备份关注用户的全部小说, so that 保存喜欢的作者的作品

#### Acceptance Criteria

1. WHEN 用户触发"备份全部", 系统 SHALL 同步该用户的所有小说到本地
2. WHEN 同步进行中, 系统 SHALL 显示同步进度
3. WHEN 同步完成, 系统 SHALL 显示同步结果统计
4. IF 同步失败, 系统 SHALL 显示错误信息并允许重试
