# InstaBox VPS 独立发布候选

- 日期：2026-11-12，Asia/Shanghai。
- VPS 已核对版本：`e8801b09340de3a3630e05065aa80dbf8c1af698`。
- 分支：`codex/instabox-vps-release`，从上述线上版本创建。
- 本记录描述本地候选与验证，线上安装、备份和真实云联调由部署记录另记。

## 内容与兼容边界

从 `f5515d6` 一期实现提取远程执行代码、专用配置和服务示例、页面及对应测试。保留 VPS 现有其余应用源码、主库 schema 和迁移；本候选没有加入作品分析第二阶段。

已有应用文件仅修改 `webapp.py` 的狭义认证例外与路由注册、`vue_components.html` 的云图标及远程入口，以及 `pyproject.toml` 的专用 CLI 入口。原 EPUB 渲染器、业务数据库和本地任务调度代码未改变。移动导航断言随新增入口更新。

同步本次复核修正：worker HTTP 每轮底层读取依据剩余总时限收紧 socket timeout；Nginx 两个 worker location 明确使用 identity 编码、HTTP/1.1 和无请求/响应缓冲。移除一个未使用 import。远程测试与浏览器复现脚本只在存在时调用作品分析清理扩展，使它们可以独立运行于线上基线。

## 本地实测

| 检查 | 结果 |
|---|---|
| `python -m pytest -q --tb=short --durations=10` | **2697 passed, 53 skipped**，278.97 秒，Windows / Python 3.11.3 |
| `python -m compileall -q src` | 通过 |
| `python -m pyflakes src` | 通过；最初发现的未使用 import 已移除 |
| `python scripts/build_tailwind_css.py --check` | 通过；原静态 CSS 无需修改 |
| 移动导航及 AI 设置 Node 测试 | **124 passed**，零失败/跳过 |
| 远程页面离线 Chromium | 390px / 1280px，各含启用与关闭，共四种状态通过 |

53 个跳过包含基线已有的浏览器 opt-in、非流式参数以及 Windows 权限/POSIX mode 情况，另有远程 Blob 符号链接权限用例；不计为通过。

浏览器复用缓存的 Vue 3.4.38，先核对既定 SHA-384 SRI，再阻止所有未预期外部请求。使用真实 Flask 登录/CSRF、临时库和合成输入，任务提交成功；所有控件至少 44×44，无横向溢出、console error 或意外外部请求。四张截图已逐张视觉复核。证据：[JSON](instabox-vps-release-ui/remote-ui-verification.json)、[390px](instabox-vps-release-ui/remote-ui-390.png)、[1280px](instabox-vps-release-ui/remote-ui-1280.png)、[关闭 390px](instabox-vps-release-ui/remote-ui-disabled-390.png)、[关闭 1280px](instabox-vps-release-ui/remote-ui-disabled-1280.png)。

独立 Linux 配置复核见 [remote-linux-verification.json](remote-linux-verification.json) 与 [复现脚本](../../../scripts/verify_remote_deployment.py)。它验证合成 TLS 配置下的 Nginx 实际代理、大小限制、429/Retry-After、并发心跳与流式上传；systemd 检查使用临时 executable fixture，实际安装路径和权限仍需部署时核对。

此候选的 20 个远程源码模块与本次 `instabox-rollout` 工作区逐字节相同。主库未读取、迁移或导出，本地验证没有访问真实 VPS/InstaBox/Pixiv。
