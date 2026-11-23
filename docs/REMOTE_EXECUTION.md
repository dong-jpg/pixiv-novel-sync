# 远程执行已退役

2026-11-23 起，main 恢复为部署主机本地执行。已移除 InstaBox 控制器、worker、上游转发、云出口、远程签到、远程 CLI 及管理页面；同步、推荐、EPUB 和 AI 调用使用本地业务入口。

NAS 安装、数据迁移、旧服务停用和备份步骤统一维护在 [Docker 部署指南](DOCKER.md)。旧 `sync.pixiv_execution`、`PIXIV_REMOTE_*` 和 `INSTABOX_API_KEY` 不再启用任何能力。更新源码不会自动关闭旧主机上的服务或终止云平台计费。

远程协议和旧运行记录可通过 Git 历史查看，不再作为当前部署要求。
