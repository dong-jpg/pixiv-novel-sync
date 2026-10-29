// Tailwind v3 配置：生成 src/pixiv_novel_sync/static/css/tailwind.css。
// 只有维护者改了模板里的类名才需要重生成（python scripts/build_tailwind_css.py），
// 安装与部署不需要 Node 或任何构建命令——生成好的 CSS 已提交进仓库。
//
// theme 原样搬自 base.html 里删掉的 Play CDN 内联 tailwind.config，改颜色或字体要同步
// docs/library-os-style-guide.md 与 base.html 的 --library-* 变量。
module.exports = {
  content: {
    relative: true,
    files: ['../src/pixiv_novel_sync/templates/*.html'],
  },
  // Play CDN 会扫运行时 DOM，CLI 只扫源码文本：只在运行时拼出来的类名（如 'bg-' + color）
  // CLI 看不见，必须登记在这里。目前模板里没有这种写法，
  // tests/test_tailwind_static_css.py 会拦下新增的拼接。
  safelist: [],
  theme: {
    extend: {
      colors: {
        brand: { 50: '#eef4ff', 100: '#dce8ff', 200: '#b8d0ff', 300: '#8fb3ff', 400: '#6f98ff', 500: '#4f7cff', 600: '#3b63db', 700: '#2d4ca8', 800: '#203675', 900: '#162345' },
        pixiv: { bg: '#f6f7fb', text: '#162033', gray: '#6b7280', light: '#8a94a6', border: '#e5e7ef' },
        library: { bg: '#f6f7fb', surface: '#ffffff', ink: '#162033', muted: '#6b7280', line: '#e5e7ef', accent: '#4f7cff', success: '#20b486' },
      },
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', '"PingFang SC"', '"Microsoft YaHei"', 'Arial', 'sans-serif'],
        serif: ['"BIZ UDMincho"', '"Noto Serif SC"', '"Source Han Serif SC"', 'SimSun', 'serif'],
      },
    },
  },
  plugins: [],
};
