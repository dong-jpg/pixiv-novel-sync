window.tailwind = window.tailwind || {};
window.tailwind.config = {
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#e6f4ff',
          100: '#bae0ff',
          200: '#7cc4fa',
          300: '#47a8f5',
          400: '#1a91f0',
          500: '#0096fa',
          600: '#0073cc',
          700: '#00559e',
          800: '#003870',
          900: '#001d42',
        },
        pixiv: {
          bg: '#f5f6f8',
          text: '#333333',
          gray: '#858585',
          light: '#999999',
          border: '#e8eaed',
        },
      },
      fontFamily: {
        sans: [
          '-apple-system', 'BlinkMacSystemFont', '"Helvetica Neue"',
          '"PingFang SC"', '"Hiragino Sans GB"', '"Microsoft YaHei"',
          'Arial', 'sans-serif',
        ],
        serif: [
          '"BIZ UDMincho"', '"Hiragino Mincho ProN"', '"Yu Mincho"',
          '"Noto Serif JP"', '"Noto Serif SC"', '"Source Han Serif SC"',
          '"SimSun"', 'serif',
        ],
      },
    },
  },
};
