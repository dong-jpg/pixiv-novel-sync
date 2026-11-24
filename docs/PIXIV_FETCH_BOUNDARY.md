# Pixiv 单本抓取结果边界

`pixiv_fetch.py` 已将单本网络抓取与本地持久化分开，现有 `_sync_novel_inner()` 使用这一结果继续执行原归档、事务、FTS 和来源更新。它不改变收藏分页、水位、来源归属、删除判定或原任务调度器。

## 固定接口

```python
dto = fetch_novel(api, novel_id, fallback_novel=novel, stop_requested=stop_requested)
validate_novel(dto, expected_novel_id=novel_id)
```

`fetch_novel()` 仅调用注入客户端的 `novel_detail` 与 `webview_novel`，不读取凭据/配置，不打开数据库或写归档。每个网络边界都检查取消；重试、认证及调用预算仍归原调度者和客户端负责。

本地 `FetchedNovel` 对象包含目标小说 ID、封闭的规范化字段、有界原始元数据、固定资产清单以及正文/元数据摘要。UTF-8 正文上限 16 MiB，全部元数据和资产清单合计上限 256 KiB；大小按字节检查，不截断。已移除远程传输的 base64 封装和协议编解码。

结果校验拒绝额外规范化字段、重复 JSON 键、容器洪泛、过深嵌套、跨小说 ID、伪摘要和不受支持的资产 URL。摘要服务于归档一致性及去重，不能代替期望小说 ID 的核对。

## 本地行为保持

原始 `meta.json/raw_json` 的合法扩展信息保留，并沿用既有稳定元数据哈希输入；访问量、收藏量等易变计数仍不参与该哈希。原封面优先级、正文规范化、缺失资产补齐、私密归档选择，以及先写文件再提交业务记录的顺序保持。

错误页和跨小说响应在写库前失败，取消继续传播为 `InterruptedError`。明确不可获取的内容保留对应分类；未知网络错误不冒充已删除或成功。错误对象仅包含固定错误码，不带正文、令牌或完整上游响应。

## 执行位置

抓取、附件下载及归档均由部署主机直接执行，保留原客户端代理、重试、限速和取消机制。远程分发、上游转发及云出口服务已移除；NAS 的网络配置见 [Docker 部署](DOCKER.md)。

测试见 `tests/test_pixiv_fetch.py` 和已有同步、增量、归档与备份回归。最新实施状态见 [STATUS](STATUS.md)。
