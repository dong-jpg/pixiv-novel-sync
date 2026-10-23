# 救援脚本排查交接（2026-10-22）

> **2026-10-23 已解决（脚本 0.9.0），本文仅作历史记录。** 根因是遮挡而非注入/时机：手机站已删作品页的 404 空壳用绝对定位的不透明通栏容器盖住了追加到 `body` 的面板；第三节表格里「`已渲染，字数 N` 但页面看不到」那一行就是答案（服务器真机页复现时状态条正显示「已渲染，字数 205815」）。同时发现旧版「无 `<main>` ⇒ 已删」会误伤所有健康正文页。现行方案与排查要点见 [RESCUE_USER_GUIDE.md](RESCUE_USER_GUIDE.md) 第 5 节「正文 / 系列页没有出现备份阅读层」。下文第四节「不会在 document-start 立即执行」「t≈14s」等是当时的观察，不再是待验证假设。
>
> 原文：新会话从这里开始。当前仓库与服务器均干净、已部署，**唯一未闭环**的是「手机端点击救援卡后正文备份面板不显示」。

## 一、当前状态

| 项 | 状态 |
|---|---|
| 分支 / HEAD | `main` @ `3ad687a`（已推送、已部署到生产） |
| 工作树 | 干净（无未提交改动） |
| 服务器 | `ubuntu@168.107.30.164`，`~/pixiv-novel-sync`，服务 active，`/api/health` 200 |
| 用户脚本版本 | **0.7.2**（`@run-at document-start`，`@match https://www.pixiv.net/*` 全域兜底） |
| 部署方式 | `./update.sh`（服务器上），我这边用 `ssh -i "C:\Users\dong\Desktop\pixiv.key"` |
| 全量测试 | 1464 passed / 6 skipped（脚本改动未破坏后端）；userscript 相关 16 项通过 |

## 二、已彻底完成并验证的部分（不要重做）

1. **失效卡片识别**（列表页）：文案「该作品无法显示」+ 官方类名 `invisible-work-container` + 文案距链接 3 层，`cardIsBroken` 上查四层；容器锚定 `div.works-item`。
2. **卡片去重**：同一卡的双嵌套 `<a>` 只注册一次（原来两个徽标叠出「重影」）。
3. **卡片重建**：封面 64×90（与正常卡片完全一致）+ 标题 + 作者 + 左上角小蓝标「拯救」；无封面退化为纯文字。
4. **封面端点**：`GET /api/rescue/v1/novels/<id>/cover` 与 `series/<id>/cover`（series 回退首章封面），bearer token 认证，路径按 cwd + 库根双重解析并做穿越校验。**服务器实测返回真实 JPEG**。
5. **缓存**：lookup 结果 10 分钟（键含 token 滚动哈希）、封面 dataURL 24 小时、`MutationObserver` + 首屏 15 秒快速轮询（每 500ms）。
6. **点击意图**：救援卡链接带 `&pixiv_rescue=1`；`consumeRescueIntent` 优先读 URL 参数，sessionStorage 兜底。
7. **启动方式**：立即 `start()`（不再挂 DOMContentLoaded——晚注入会导致永不触发）；`document-start` 下 body/head 缺失的兜底。
8. **可见诊断**：右下角角标「救援 <版本>」6 秒；顶层 try/catch 红条 `救援脚本出错：<原因>`；GM API 缺失时 500ms×10 重试。
9. **空指针守卫**：`renderChapterDirectory` 的 `panel.contains(viewer)`、`mountPoint.parentElement`、路由回调 try/catch。

## 三、唯一未解决的问题

**症状**：手机上直接打开 `https://www.pixiv.net/novel/show.php?id=29337554&pixiv_rescue=1`，右下角出现「救援 0.7.1/0.7.2」角标（脚本确实运行），**无错误条**，但**正文备份面板不出现**；角标 6 秒后按设计消失。

**已排除**：
- 服务端数据：`db.get_rescue_novel(29337554)` 返回 `rescue_state=success`、`text_raw` 205815 字，完全正常。
- 桌面 E2E（真实登录态 + 手机 UA + 模拟 GM）：`panel: true`、状态「已渲染，字数 5」，**代码路径本身跑得通**。
- 页面路由判断：`/novel/show.php`、`/novel/series/*`、书签页、追更页、`bookmark_new.php` 全部命中正确，`/novel/`、`/novel/new` 等不命中。
- 页面文案：URL 带 `pixiv_rescue=1` 时完全不看文案，直接 `renderBackup()`。

**最新一次部署（0.7.2）加了分步状态条**，就是为解决这个：`handleNovelPage` 的每个节点会画在页面顶部蓝色条上。

**下一步只需用户反馈状态条内容**（这是 0.7.2 的唯一目的）：

| 状态条显示 | 结论 / 下一步 |
|---|---|
| 看不到任何状态条 | `handleNovelPage` 未被调用 → 当前 URL 不是 `/novel/show.php`（用户上次截图底部地址栏是 `pixiv.net/novel/` 信息流页，需确认打开的是完整链接） |
| `novel 页 id=...` 后停住 | `consumeRescueIntent` / `isNovelPageHealthy` 分支卡住 |
| `请求备份 <id>` 后停住 | 手机管理器沙箱的 `GM_xmlhttpRequest` 行为差异（可能不支持或跨域策略不同） |
| `备份请求失败: xxx` | 网络/Token/证书问题，按错误码排查 |
| `代次/ID 已变，放弃渲染 (...)` | Pixiv SPA 在渲染期间改写了 URL 导致 generation 失效 |
| `已渲染，字数 N` 但页面看不到 | 面板被 Pixiv 后续渲染覆盖，或挂载点位置问题 |

## 四、关键背景（新会话容易踩的坑）

- 用户的浏览器是**移动端内置脚本管理器**（非 Tampermonkey），脚本跑在沙箱里：**页面控制台读不到 `window.__pixivRescueLoaded`**，返回 `undefined` 不能证明脚本没运行。这曾误导排查好几轮。
- 该环境的脚本管理器**不会在 `document-start` 立即执行**（实测角标出现说明脚本运行，但时机未知）。
- Pixiv 已删作品的正文页是 **SPA 空壳**：`<main>` 不存在，「尚无此页」在 `load` 事件之后才渲染（实测 t≈14s）。因此任何「等页面出现失效文案再渲染」的策略都不可靠——这也是为什么改用 URL 标记 + 立即尝试渲染。
- 用户偏好：中文回复；改动要端到端验证（服务器上有真实登录 Cookie，可用 Playwright 复现）；每次改完都要部署（`./update.sh`）并确认 `/api/health` 200。
- 生产库：`data/state/pixiv_sync.db`；救援 token 前缀 `rsq_8MQ7C3X4`（完整令牌只在用户浏览器里）。
- 服务器 `.env` 有 `PIXIV_WEB_COOKIE`（可登录态访问 Pixiv 页面做 E2E）。
- 测试脚本模板：`/tmp/e2e_*.py`（服务器上），思路是 Playwright + 真实 cookie + `page.add_init_script` 模拟 GM API。

## 五、验证与部署命令

```bash
# 本地测试
python -m compileall -q src
python -X utf8 -m pytest tests/test_rescue_userscript.py tests/test_rescue_userscript_install.py -q
node --check userscripts/pixiv-rescue.user.js

# 推送 + 部署
git push origin main
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 \
  "cd ~/pixiv-novel-sync && ./update.sh 2>&1 | grep -E '更新成功|HEAD'"
# 启动约需 90-100 秒，之后验证：
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 \
  "systemctl is-active pixiv-novel-sync && curl -s -o /dev/null -w 'health: %{http_code}\n' http://127.0.0.1:5011/api/health"
```

## 六、调试完记得做的事

`showStep()` 分步状态条是**临时诊断代码**，问题定位后应移除（`showBootBadge` 可保留，对用户无害且便于以后排查）。移除后同步更新 `tests/test_rescue_userscript.py` 中对应的锁定断言，并把本轮的 DOM/环境结论补进 `docs/RESCUE_USER_GUIDE.md` 的故障排查章节。
