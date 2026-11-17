# Pixiv Novel Sync Logo 设计说明

当前 README 使用 `assets/main-logo.svg`（书页与同步环），112×112 展示，文字标题与图标分离。`assets/logo.svg` 与 `assets/logo-mark.svg` 是保留的 Writer's Desk 品牌素材，含写作寓意；不据旧提案替换当前 README。

## 设计方向

当前 Logo 采用 Writer's Desk 方向：像一个安静的本地编辑台，而不是高饱和的科技渐变标志。它需要同时表达三件事：

1. 小说归档：折角文稿代表本地资料库和可保存的文本。
2. AI 创作：右下角编辑笔尖代表续写、润色和章节工作流。
3. 偏好推荐：蓝绿色文本线保留一点“整理、发现、筛选”的系统感。

## 颜色

| 用途 | 色值 |
|------|------|
| 暖色背景 | #FFF7EF |
| 纸张 | #FFFFFF |
| 主文字/描边 | #2A211B |
| 砖红主色 | #B75A3C |
| 蓝绿辅助色 | #4A7C74 |
| 柔和阴影 | #F3DFD0 |

整体避免蓝紫粉渐变，保持温暖、克制和可长期维护的 GitHub 项目气质。

## 文件

- assets/main-logo.svg：当前 README 图标，表达私人书库和同步。
- assets/logo.svg：保留的完整 Writer's Desk 图，包含 mark、项目名、中文定位语和英文短标语。
- assets/logo-mark.svg：仅包含折角文稿和编辑标记，适合 GitHub avatar、favicon 或小尺寸引用。

这些 SVG 都是静态文件，不依赖外部字体、脚本、图片或动画。标准 SVG namespace 仅用于稳定渲染，不代表远程资源引用。

## 使用建议

- README 首屏保持现有 main-logo.svg 的 112px 图标与可访问文字标题。
- 小尺寸头像、图标按钮或 favicon 候选建议使用 assets/logo-mark.svg。
- 深色背景上使用时，应优先放在浅色容器内，不要直接反白主图。
- 不要重新加入动态效果、霓虹渐变、emoji 元素或 AI 生成图噪点。

## 非目标

- 不处理 GitHub Social Preview 大图。
- 不创建外部 CDN 资源。
- 不把 Logo 变成复杂插画；32px 下能识别轮廓比装饰细节更重要。
