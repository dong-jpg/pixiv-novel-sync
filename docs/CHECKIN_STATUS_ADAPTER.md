# 单站签到状态适配底座

该模块实现 `checkin.status.v1` 的 Python 请求、结果和只读网关边界，尚未加入远程任务注册表、HTTP 路由或 worker 派发。它不自动签到，不安装或启用 `daily-checkin`，也不迁移真实账户、浏览器会话或回执数据库。

## 调用与证据

入口为 `remote.checkin_status.execute_checkin_status(request, gateway, clock=...)`。请求字段严格限定为 `protocol=1`、`site_id`、`config_version`、`business_day`、`timezone`、`expected_identity`、`session_binding`；额外 URL、路径、命令、凭据及 `execute` 参数均拒绝。业务日不是运行时重新生成的签到日期：只在冻结的站点日内查询，过期不打开网关，查询跨日也返回 `expired`。

`expected_identity` 是由配置侧独立密钥、站点 ID 和绑定 UID 计算的 HMAC-SHA256。消息仅携带代号，不含原 UID 或 HMAC 密钥。`session_binding` 是部署方维护的会话代次代号，不是 Cookie、登录 token 或远程 worker 授权凭据。网关在查询前、各读取边界及返回后核对配置、身份和会话绑定；结果还包含绑定完整请求的 `request_sha256`。

结果只含固定协议、站点、原业务日、请求摘要、状态、身份匹配布尔值/空值、证据来源及固定原因码。状态为 `due/already_done/login_required/uncertain/unavailable/expired`。`pending/unknown/uncertain` 不构成已签到证据；日期不符或身份未核验也不能报告本次查询成功。

| evidence | 含义 |
|---|---|
| `live_status` | 本次通过身份核验并读取站点状态；`identity_match=true` 才能报告 `due` 或实时 `already_done` |
| `local_receipt` | 原项目已有该站点、绑定 UID、业务日的明确成功回执；`identity_match=null`，不声称本次重新验证了浏览器身份 |
| `none` | 没有可采用的实时状态或本地确认回执 |

## 固定项目网关

`DailyCheckinRunnerGateway` 使用注入的 store、浏览器 transport factory 和独立配置。它仅调用公共 `Runner.run([site_id], execute=False)`，保留原项目同机进程锁，不调用内部 `run_site()`。所需 Runner/Adapter 在首次使用时才延迟导入，不把 `daily-checkin` 增加为 Pixiv 的必装依赖。

网关为自己的 transport 和 adapter 实例安装守卫：禁止 `submit`、`fresh_login`、挑战求解入口以及 GET/HEAD 以外的请求。守卫单独记录越界尝试，即使原 Runner 捕获异常后返回成功，外层仍拒绝该结果。这里没有全局 requests/浏览器 monkeypatch；如某站只读状态接口需要 POST，该站在当前网关下不兼容，不能自行扩大权限。

“只读”指**不向站点提交签到或重新登录**。现有 `Runner.run(execute=False)` 仍可更新本地回执、冷却和浏览器状态，打开已配置站点页面也会产生正常浏览器访问；该模块不承诺浏览器页面脚本完全没有副作用。

## 实际运行前仍需配置与验收

部署方须显式准备独立 `CheckinLocalConfig(data_dir, identity_key, session_bindings)`，然后主动调用 `create_local_gateway()` 才初始化项目私有状态。模块不读取 `.env`、搜索 profile、提取凭据或自动登录。需先确定：

- 可选 `daily-checkin` 版本与依赖（现有项目要求 Python 3.11+）、非 root Chromium 沙箱和已授权的私有数据目录。
- 单站绑定身份、配置版本与已经确认的站点时区；`timezone_verified=false` 时不访问站点。
- 稳定的独立 HMAC 密钥和会话代次维护机制。登录会话变化必须更新绑定；当前模块不监测或推断 profile 中的实际登录变化。
- 浏览器 profile/回执权威位置及备份策略，站点状态查询语义与日期回执的真实验收。不能复制私有状态后让多端各自运行。

未来真正的签到提交仍需独立的 VPS 副作用台账、冻结计划日期及未知结果核对协议；不能复用 EPUB 的自动重试分类。当前合成测试仅验证本模块边界和固定网关约定，不代表任何真实站点已验收。
