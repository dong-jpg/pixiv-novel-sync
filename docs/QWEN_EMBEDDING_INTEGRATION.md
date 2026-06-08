# Qwen3-Embedding-8B 接入方案

当前已为 AI 写作工作室的项目记忆检索增加 OpenAI-compatible embedding 接口。配置以下环境变量后，章节摘要、关键事件索引和项目上下文搜索会优先使用远程 embedding 检索；未配置时仍回退到现有 TF-IDF 检索。

```env
PIXIV_NOVEL_SYNC_EMBEDDING_BASE_URL=https://your-provider.example/v1
PIXIV_NOVEL_SYNC_EMBEDDING_API_KEY=your_api_key
PIXIV_NOVEL_SYNC_EMBEDDING_MODEL=Qwen3-Embedding-8B
PIXIV_NOVEL_SYNC_EMBEDDING_TIMEOUT=60
```

兼容别名：

```env
QWEN_EMBEDDING_BASE_URL=https://your-provider.example/v1
QWEN_EMBEDDING_API_KEY=your_api_key
QWEN_EMBEDDING_MODEL=Qwen3-Embedding-8B
```

要求服务端支持 OpenAI embeddings 响应格式：

```json
{
  "data": [
    {"index": 0, "embedding": [0.1, 0.2, 0.3]}
  ]
}
```

## 当前行为

- 配置了 `BASE_URL` 和 `API_KEY` 时，系统优先创建远程 embedding 检索器。
- 远程检索器初始化失败时，自动回退到 TF-IDF，不阻断 AI 写作功能启动。
- 同一章节的摘要和关键事件内容未变化时，会复用已有向量索引，避免重复消耗 API 调用。
- 新索引以 float32 BLOB 存储；旧的 JSON 向量索引仍可被读取，便于平滑升级。
- 远程 API 在实际索引或搜索过程中报错时，错误会继续暴露给调用方，方便发现网络、额度或鉴权问题。

## 隐私与成本

启用远程 embedding 后，以下内容会发送到配置的 embedding 服务商：

- AI 写作项目的章节摘要
- 章节关键事件
- 搜索查询文本

不要把 Pixiv refresh token、Cookie、API key 或其他凭据写入摘要、关键事件或查询文本。私密收藏、创作设定、角色关系等文本也可能具有隐私敏感性，启用前应确认服务商的数据处理策略。

为控制成本：

- 优先索引摘要和关键事件，不要默认索引完整正文。
- 内容未变化时依赖 content hash 复用已有向量。
- 模型或 provider 改变后再重建索引。

## 索引与重建

远程向量索引保存在主数据库同目录的 `ai_retrieval_api_vec.db`。该索引可重建，不是唯一数据源；备份时如果希望保留检索速度，可以和主数据库一起备份。

需要重建索引的情况：

- 更换 embedding 模型。
- 更换 provider 且向量维度或归一化策略不同。
- 大量章节摘要或关键事件被批量修改。
- 想清理旧 JSON 向量或旧模型索引。

## 推荐落地顺序

1. 写作项目记忆：对章节摘要、关键事件、伏笔和项目状态做向量检索，生成下一章时召回最相关上下文。
2. 本地小说语义搜索：按剧情、关系、风格、情绪查询本地归档，不再依赖标题和标签完全命中。
3. 偏好画像增强：把收藏小说和候选小说向量化，和现有标签/热度/长度评分融合。
4. 去重与聚类：识别主题相似、风格相近、重复归档或系列变体。
5. UI 管理：把 embedding provider 做进 dashboard 配置页，复用现有 AI provider 的密钥加密能力。
