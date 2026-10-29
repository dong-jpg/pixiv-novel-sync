// 阅读页正文标记：运行真实模板里的脚本（pixiv_markup.html + dashboard_novel_detail.html）。
// 不带参数：共享夹具、XSS 白名单、模糊输入、线性时间、阅读页绑定。
// 带一个参数（Python 写出的语料 JSON）：只做 JS / Python 解析结果逐项比对。
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const templates = path.join(root, 'src/pixiv_novel_sync/templates');
const read = name => fs.readFileSync(path.join(templates, name), 'utf8');
const scripts = html => [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
// vm 里建的对象原型不同，先过一遍 JSON 再和夹具比
const plain = value => JSON.parse(JSON.stringify(value));

function loadMarkup() {
  const window = {};
  vm.runInNewContext(scripts(read('pixiv_markup.html'))[0], { window }, { filename: 'pixiv_markup.html' });
  return window.pixivMarkup;
}

const corpusPath = process.argv[2];
if (corpusPath) {
  test('JS parser matches Python on the fuzz corpus', () => {
    const markup = loadMarkup();
    const corpus = JSON.parse(fs.readFileSync(corpusPath, 'utf8'));
    assert.ok(corpus.length > 0);
    for (const item of corpus) {
      assert.deepStrictEqual(plain(markup.parse(item.text)), item.pages, JSON.stringify(item.text));
    }
  });
} else {
  defineReaderTests();
}

// 渲染结果只允许这些标签和属性；属性值逐个校验
const TAGS = {
  p: ['class'], h2: ['class'], div: ['class', 'id', 'aria-hidden'], span: ['class'],
  a: ['class', 'href', 'target', 'rel', 'data-pns-page'],
  ruby: [], rt: [], rp: [], b: [], i: [],
};
const CLASSES = new Set(['empty-line', 'pns-chapter', 'pns-page-anchor', 'pns-page-break', 'pns-image-missing',
  'pns-link', 'pns-jump-disabled', 'pns-emphasis', 'pns-emphasis-sesame']);
const ATTR_VALUE = {
  id: v => /^pns-page-[0-9]+$/.test(v),
  href: v => /^https?:\/\/[^\s"'<>`]+$/i.test(v) || /^#pns-page-[0-9]+$/.test(v),
  target: v => v === '_blank',
  rel: v => /^noopener noreferrer( nofollow)?$/.test(v),
  'data-pns-page': v => /^[0-9]+$/.test(v),
  'aria-hidden': v => v === 'true',
  class: v => v.split(' ').every(c => CLASSES.has(c)),
};

function assertWhitelisted(html, label = '') {
  const tagRe = /<(\/?)([a-zA-Z0-9]+)((?:\s+[a-z-]+="[^"]*")*)\s*>/g;
  let rest = html;
  for (const m of html.matchAll(tagRe)) {
    const [, closing, name, attrs] = m;
    assert.ok(Object.hasOwn(TAGS, name), `${label}: tag <${name}> not allowed`);
    if (closing) assert.equal(attrs, '', `${label}: closing tag with attributes`);
    for (const a of attrs.matchAll(/\s+([a-z-]+)="([^"]*)"/g)) {
      assert.ok(TAGS[name].includes(a[1]), `${label}: <${name} ${a[1]}> not allowed`);
      assert.ok(ATTR_VALUE[a[1]](a[2]), `${label}: bad ${a[1]}="${a[2]}"`);
    }
  }
  rest = rest.replace(tagRe, '');
  // 去掉白名单标签后只剩文字：不能再有尖括号，& 只能是转义序列
  assert.ok(!/[<>]/.test(rest), `${label}: raw angle bracket left in text: ${rest.slice(0, 200)}`);
  assert.ok(!/&(?!(?:amp|lt|gt|quot|#039);)/.test(rest), `${label}: unescaped & in text`);
}

function fuzzCorpus(count, seed) {
  const pieces = JSON.parse(fs.readFileSync(path.join(root, 'tests/fixtures/pixiv_markup_fuzz_pieces.json'), 'utf8'));
  let state = seed;
  const next = () => { state = (state * 48271) % 2147483647; return state; };
  const out = [];
  for (let n = 0; n < count; n += 1) {
    let text = '';
    const size = 1 + (next() % 18);
    for (let k = 0; k < size; k += 1) text += pieces[next() % pieces.length];
    out.push(text);
  }
  return out;
}

function mountReader(novel) {
  let state;
  const elements = new Map();
  const window = {
    location: { pathname: '/dashboard/novels/1', search: '', hash: '', origin: 'http://localhost', href: '' },
    scrollY: 0, innerHeight: 600,
    scrollTo({ top }) { this.scrollY = top; }, addEventListener() {}, removeEventListener() {},
    toast() {}, errorText: data => data.error || 'failed', confirm: () => true,
    csrfFetch: async () => ({ ok: true, json: async () => ({ ok: true }) }),
  };
  const document = {
    documentElement: { scrollHeight: 3000 }, title: '', referrer: '',
    getElementById: id => elements.get(id) || null,
  };
  const context = vm.createContext({
    window, document, location: window.location, URL, URLSearchParams, AbortController, console,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setTimeout: () => 0, clearTimeout() {},
    fetch: async () => ({ ok: true, json: async () => novel }),
    initVueApp: app => { state = app.setup(); },
    Vue: {
      ref: value => ({ value }), reactive: value => value, computed: fn => ({ get value() { return fn(); } }),
      watch() {}, onMounted() {}, onUnmounted() {}, nextTick: () => Promise.resolve(),
    },
  });
  for (const name of ['navigation_helpers.html', 'pixiv_markup.html', 'dashboard_novel_detail.html']) {
    for (const code of scripts(read(name))) vm.runInContext(code, context, { filename: name });
  }
  state.novel.value = novel;
  return { state, window, elements };
}

function defineReaderTests() {
  const markup = loadMarkup();
  const cases = JSON.parse(fs.readFileSync(path.join(root, 'tests/fixtures/pixiv_markup_cases.json'), 'utf8'));

  test('JS parser matches every shared fixture', () => {
    for (const c of cases) assert.deepStrictEqual(plain(markup.parse(c.text)), c.pages, c.name);
  });

  test('XSS payloads never become markup', () => {
    const payloads = [
      '<script>alert(1)</script>',
      '"><img src=x onerror=alert(1)>',
      "'><svg onload=alert(1)>",
      '[[jumpuri:点我 > javascript:alert(document.cookie)]]',
      '[[jumpuri:x > JaVaScRiPt:alert(1)]]',
      '[[jumpuri:x > https://e.com/"onmouseover="alert(1)]]',
      '[[jumpuri:x > https://e.com/\'onfocus=\'alert(1)]]',
      '[[jumpuri:x > data:text/html,<script>alert(1)</script>]]',
      '[[jumpuri:<b>x</b> > https://e.com/]]',
      '[[rb:<script> > </script>]]',
      '[chapter:<img src=x onerror=alert(1)>]',
      '[pixivimage:1"><script>alert(1)</script>]',
      '[uploadedimage:<script>]',
      '[b:<i onclick=x>]',
      '[[emphasismark:<x> > "]]',
      '[jump:1"onclick="x]',
      '&lt;script&gt; &amp; &',
    ];
    for (const payload of payloads) {
      const html = markup.renderHtml(payload);
      assertWhitelisted(html, payload);
      assert.ok(!/<script/i.test(html), payload);
      assert.ok(!/href="\s*javascript:/i.test(html), payload);
      // 文字里的「<」都已转义，/<[^>]*>/ 只会匹配到真正的标签；正文里字面的 onerror= 无害
      for (const tag of html.match(/<[^>]*>/g) || []) assert.ok(!/\son[a-z]+=/i.test(tag), `${payload}: ${tag}`);
    }
    assert.match(markup.renderHtml('<script>alert(1)</script>'), /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
    assert.equal(markup.renderHtml('[[jumpuri:点我 > javascript:alert(1)]]').includes('<a'), false);
  });

  test('every marker renders without raw brackets', () => {
    const html = markup.renderHtml([
      '[[rb:漢字 > かんじ]]を[b:強調]して[i:斜体]、[[emphasismark:着重 > ﹅]]。',
      '[chapter:第一章 [[rb:始 > はじ]]まり]',
      '[[jumpuri:公式 > https://www.pixiv.net/?a=1&b=2]] [jump:2] [jump:9]',
      '[pixivimage:12345-2][uploadedimage:999]',
      '[newpage]二页',
    ].join('\n'));
    assertWhitelisted(html, 'all markers');
    for (const raw of ['[newpage]', '[chapter:', '[[rb:', '[[jumpuri:', '[jump:', '[pixivimage:', '[uploadedimage:',
      '[b:', '[i:', '[[emphasismark:']) {
      assert.ok(!html.includes(raw), raw);
    }
    assert.match(html, /<ruby>漢字<rp>\(<\/rp><rt>かんじ<\/rt><rp>\)<\/rp><\/ruby>/);
    assert.match(html, /<h2 class="pns-chapter">第一章 <ruby>始/);
    assert.match(html, /<a class="pns-link" href="https:\/\/www\.pixiv\.net\/\?a=1&amp;b=2" target="_blank" rel="noopener noreferrer nofollow">公式<\/a>/);
    assert.match(html, /<a class="pns-link" href="#pns-page-2" data-pns-page="2">跳到第 2 页<\/a>/);
    assert.match(html, /<span class="pns-jump-disabled">跳到第 9 页<\/span>/);
    assert.match(html, /<div class="pns-page-break" id="pns-page-2">2 \/ 2 页<\/div>/);
    assert.match(html, /此处原有插图（私人备份未收录插图） <a class="pns-link" href="https:\/\/www\.pixiv\.net\/artworks\/12345"/);
    assert.match(html, /<span class="pns-emphasis pns-emphasis-sesame">着重<\/span>/);
  });

  test('plain text keeps the old one-<p>-per-line layout', () => {
    assert.equal(markup.renderHtml('　一行目\n\n二行目'),
      '<div class="pns-page-anchor" id="pns-page-1" aria-hidden="true"></div>\n<p>一行目</p>\n<p class="empty-line"></p>\n<p>二行目</p>');
  });

  test('fuzz corpus stays inside the whitelist', () => {
    for (const text of fuzzCorpus(4000, 20261029)) assertWhitelisted(markup.renderHtml(text), JSON.stringify(text));
  });

  test('adversarial bodies render in linear time', () => {
    for (const text of ['a' + '\n'.repeat(100000) + 'b', '[b:'.repeat(100000), '[[rb:x'.repeat(50000),
      '['.repeat(100000) + ']'.repeat(100000), '[pixivimage:'.repeat(50000) + ']'.repeat(50000),
      ' '.repeat(200000) + 'x', 'x[newpage]'.repeat(30000)]) {
      const started = Date.now();
      const html = markup.renderHtml(text);
      assert.ok(Date.now() - started < 3000, text.slice(0, 12));
      assert.ok(html.length > 0);
    }
    const blank = markup.renderHtml('上' + '\n'.repeat(10000) + '下');
    assert.equal(blank.match(/empty-line/g).length, 3, 'ten thousand blank lines collapse to three');
  });

  test('reader page renders the body through the shared renderer', () => {
    const html = read('dashboard_novel_detail.html');
    assert.match(html, /\{% include "pixiv_markup\.html" %\}/);
    assert.match(html, /class="reader-body" :class="'fs-' \+ fontSize" v-html="formattedContent" @click="onBodyClick"/);
    assert.match(html, /window\.pixivMarkup\.renderHtml\(text\)/);
    const { state } = mountReader({ novel_id: 1, title: 't', text_raw: '[[rb:漢字 > かんじ]]<script>x</script>[newpage]二' });
    const out = state.formattedContent.value;
    assert.match(out, /<ruby>漢字/);
    assert.match(out, /&lt;script&gt;x&lt;\/script&gt;/);
    assert.match(out, /pns-page-break/);
    assertWhitelisted(out, 'reader');
    const fallback = mountReader({ novel_id: 1, title: 't', text_raw: '', text_markdown: '[b:粗]' });
    assert.match(fallback.state.formattedContent.value, /<b>粗<\/b>/);
  });

  test('in-page jump scrolls without touching location.hash', () => {
    const { state, window, elements } = mountReader({ novel_id: 1, title: 't', text_raw: 'a[newpage]b[jump:2]' });
    const scrolled = [];
    elements.set('pns-page-2', { scrollIntoView: options => scrolled.push(options) });
    let prevented = 0;
    const link = { getAttribute: name => (name === 'data-pns-page' ? '2' : null) };
    state.onBodyClick({ target: { closest: selector => (selector === 'a[data-pns-page]' ? link : null) },
      preventDefault: () => { prevented += 1; } });
    assert.equal(prevented, 1);
    assert.deepEqual(plain(scrolled), [{ block: 'start', behavior: 'auto' }]);
    assert.equal(window.location.hash, '');
    // 外链与普通文字的点击不拦
    state.onBodyClick({ target: { closest: () => null }, preventDefault: () => { prevented += 1; } });
    state.onBodyClick({ target: {}, preventDefault: () => { prevented += 1; } });
    assert.equal(prevented, 1);
    // 目标不在页面上也不能落回默认跳转去改 hash
    const missing = { getAttribute: () => '9' };
    state.onBodyClick({ target: { closest: () => missing }, preventDefault: () => { prevented += 1; } });
    assert.equal(prevented, 2);
    assert.equal(scrolled.length, 1);
  });
}
