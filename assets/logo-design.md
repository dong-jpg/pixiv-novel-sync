# Pixiv Novel Sync Logo 设计方案

## 设计理念

Logo 应体现项目的三大核心功能：
1. **同步归档** - 书籍/收藏的概念
2. **AI 创作** - 智能/创新的概念
3. **智能推荐** - 发现/连接的概念

## 方案一：书籍 + 云同步 + AI 光环

```
     ___
    /   \  ← AI 光环
   |  📖  |  ← 书籍图标
   | ⟳   |  ← 同步箭头
    \___/
```

- **主色调**: 蓝色 (#4A90E2) - 代表知识和技术
- **辅色**: 紫色渐变 (#9B59B6) - 代表 AI 和智能
- **图标元素**: 书本 + 圆形箭头 + 光晕效果

## 方案二：P 字母变形

```
┌─────┐
│ P   │← Pixiv 首字母
│  AI │← AI 元素
└─────┘
```

- 将 P 字母设计成书架/存储柜的形状
- 内部嵌入 AI 芯片图案
- 简洁现代，易于识别

## 方案三：三角融合（推荐）

```
    △  ← 发现/推荐
   ◇ ◇  ← 双层结构
  △───△ ← 同步/连接
```

- **三个三角形**代表三大功能
- **交汇点**代表数据融合
- **渐变色**：蓝→紫→粉，呼应 Pixiv 品牌色

## Logo 文字排版

### 中文
```
Pixiv Novel Sync
小说归档 · AI 创作 · 智能推荐
```

### 英文
```
PIXIV NOVEL SYNC
Archive · Create · Discover
```

## 在线生成工具推荐

1. **Canva** - 免费模板丰富
2. **Logo Maker** - AI 自动生成
3. **Figma** - 专业设计工具
4. **使用 AI 生成**: 可以使用 DALL-E/Midjourney 生成

## 提示词示例（用于 AI 生成）

```
A modern, minimalist logo for a software project called "Pixiv Novel Sync". 
The logo should combine three elements: 
1) A book or document icon representing archiving
2) A circular sync arrow representing synchronization
3) A subtle AI neural network pattern or glow effect representing intelligence

Color scheme: gradient from blue (#4A90E2) to purple (#9B59B6) to pink (#E91E63)
Style: flat design, clean lines, tech-focused
Format: square icon suitable for GitHub avatar, 512x512px
```

## SVG 代码模板（简化版）

下面是一个可以直接使用的 SVG logo：

```svg
<svg width="200" height="200" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="gradient" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:#4A90E2;stop-opacity:1" />
      <stop offset="50%" style="stop-color:#9B59B6;stop-opacity:1" />
      <stop offset="100%" style="stop-color:#E91E63;stop-opacity:1" />
    </linearGradient>
  </defs>
  
  <!-- 外圈 - 代表同步 -->
  <circle cx="100" cy="100" r="80" fill="none" stroke="url(#gradient)" stroke-width="8" stroke-dasharray="15 5"/>
  
  <!-- 书本图标 -->
  <rect x="70" y="70" width="60" height="60" rx="5" fill="url(#gradient)" opacity="0.8"/>
  <rect x="75" y="75" width="50" height="50" rx="3" fill="white"/>
  
  <!-- AI 符号 -->
  <text x="100" y="110" font-family="Arial" font-size="24" font-weight="bold" text-anchor="middle" fill="url(#gradient)">AI</text>
  
  <!-- 同步箭头 -->
  <path d="M 140 90 A 30 30 0 1 1 140 110" fill="none" stroke="url(#gradient)" stroke-width="3" marker-end="url(#arrowhead)"/>
  
  <defs>
    <marker id="arrowhead" markerWidth="10" markerHeight="10" refX="5" refY="5" orient="auto">
      <polygon points="0 0, 10 5, 0 10" fill="url(#gradient)" />
    </marker>
  </defs>
</svg>
```

保存为 `assets/logo.svg` 即可使用。

## 使用示例

### README 头部
```markdown
<div align="center">
  <img src="assets/logo.svg" alt="Pixiv Novel Sync" width="200"/>
  <h1>Pixiv Novel Sync</h1>
  <p>小说归档 · AI 创作 · 智能推荐</p>
</div>
```

### GitHub Social Preview
- 尺寸: 1280x640px
- 将 logo 放在左侧，右侧添加项目标语和关键功能
