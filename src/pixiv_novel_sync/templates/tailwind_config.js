window.tailwind = window.tailwind || {};
window.tailwind.config = {
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f2f9ff',
          100: '#e6f3ff',
          200: '#bfdfff',
          300: '#99cbff',
          400: '#4da6ff',
          500: '#0096fa', // Pixiv primary blue
          600: '#0087e1',
          700: '#0070bb',
          800: '#005a96',
          900: '#00497b',
          950: '#002f4d',
        },
        pixiv: {
          bg: '#f5f6f8',
          text: '#333333',
          gray: '#999999'
        }
      },
      fontFamily: {
        sans: ['"Helvetica Neue"', 'Helvetica', '"PingFang SC"', '"Hiragino Sans GB"', '"Microsoft YaHei"', '微软雅黑', 'Arial', 'sans-serif'],
      }
    }
  }
};
