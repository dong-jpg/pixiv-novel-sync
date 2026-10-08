// Pure Node VM unit tests of the real template scripts, not a browser/layout test.
// Vue lifecycle and DOM boundaries are small test doubles. No network is allowed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const root = path.resolve(__dirname, '../src/pixiv_novel_sync/templates');
const componentsSource = fs.readFileSync(path.join(root, 'vue_components.html'), 'utf8');
const baseSource = fs.readFileSync(path.join(root, 'base.html'), 'utf8');
const json = (data, ok = true, status = ok ? 200 : 503) => ({ok, status, json: async () => data});
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const assertFocus = (h, target) => assert.ok(h.document.activeElement === target,
  `Expected focus on ${target.name}, got ${h.document.activeElement?.name}`);

function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    dispatch(type, event = {}) {
      event.type = type;
      event.preventDefault = () => {event.defaultPrevented = true;};
      event.stopPropagation = () => {event.stopped = true;};
      for (const fn of [...(listeners.get(type) || [])]) fn(event);
      return event;
    },
    listenerCount() { return [...listeners.values()].reduce((n, set) => n + set.size, 0); },
  };
}

function style() {
  const values = new Map(), priorities = new Map();
  const api = {
    getPropertyValue: key => values.get(key) || '',
    getPropertyPriority: key => priorities.get(key) || '',
    setProperty(key, value, priority = '') {values.set(key, value); priorities.set(key, priority);},
    removeProperty(key) {values.delete(key); priorities.delete(key);},
  };
  return new Proxy(api, {
    get: (target, key) => key in target ? target[key] : api.getPropertyValue(key),
    set: (_target, key, value) => {api.setProperty(key, value); return true;},
  });
}

function harness() {
  const document = Object.assign(eventTarget(), {activeElement: null});
  function element(name, {tabIndex = -1, hidden = false, disabled = false} = {}) {
    const attributes = new Map();
    const el = {
      name, tabIndex, hidden, disabled, inert: false, isConnected: true,
      style: style(), children: [], parentElement: null,
      append(child) {child.parentElement = el; el.children.push(child); return child;},
      contains(target) {return target === el || el.children.some(child => child.contains(target));},
      setAttribute(key, value) {attributes.set(key, String(value));},
      getAttribute: key => attributes.get(key) ?? null,
      removeAttribute(key) {attributes.delete(key);},
      hasAttribute: key => attributes.has(key),
      matches(selector) {return selector === ':disabled' && el.disabled;},
      closest() {
        for (let node = el; node; node = node.parentElement) {
          if (node.inert || node.getAttribute('aria-hidden') === 'true') return node;
        }
        return null;
      },
      getClientRects() {
        for (let node = el; node; node = node.parentElement) if (node.hidden) return [];
        return [{}];
      },
      getBoundingClientRect: () => ({height: 84}),
      querySelectorAll() {
        const descendants = child => [child, ...child.children.flatMap(descendants)];
        return el.children.flatMap(descendants).filter(child => child.tabIndex >= 0);
      },
      focus() {
        if (!el.isConnected || el.closest() || el.disabled) return;
        document.activeElement = el;
        document.dispatch('focusin', {target: el});
      },
    };
    return el;
  }
  document.body = element('body');
  document.documentElement = element('html');
  document.documentElement.clientWidth = 1000;
  const appRoot = document.body.append(element('app'));
  const opener = appRoot.append(element('opener', {tabIndex: 0}));
  document.activeElement = opener;
  document.getElementById = id => id === 'app' ? appRoot : null;
  const media = Object.assign(eventTarget(), {matches: true});
  const observations = [];
  class ResizeObserver {
    constructor(callback) {this.callback = callback; this.disconnected = false; observations.push(this);}
    observe(target) {this.target = target; this.callback([{target}]);}
    disconnect() {this.disconnected = true;}
  }
  const window = Object.assign(eventTarget(), {
    innerWidth: 1016, location: {href: '/dashboard', pathname: '/dashboard', search: ''},
    matchMedia: () => media, ResizeObserver,
    getComputedStyle: el => ({visibility: el.hidden ? 'hidden' : 'visible', paddingRight: '5px'}),
    csrfFetch: async () => json({ok: true}),
    errorText: (data, response) => data?.detail || data?.error || data?.message || `HTTP ${response?.status ?? '?'}`,
  });
  const components = new Map();
  const order = [];
  const app = {
    config: {globalProperties: {}},
    component(name, definition) {components.set(name, definition); order.push(name); return app;},
    mount(target) {order.push(['mount', target]); return {};},
  };
  let current;
  const Vue = {
    ref: value => ({value}), computed: getter => ({get value() {return getter();}}),
    onMounted: fn => current.mounted.push(fn),
    onBeforeUnmount: fn => current.unmounted.push(fn),
    onUnmounted: fn => current.unmounted.push(fn),
    nextTick: callback => Promise.resolve().then(callback),
    watch(getter, fn, options = {}) {
      const watcher = {getter, fn, value: getter()};
      current.watchers.push(watcher);
      if (options.immediate) fn(watcher.value);
      return () => {current.watchers = current.watchers.filter(item => item !== watcher);};
    },
    createApp(options) {app.options = options; return app;},
  };
  const context = vm.createContext({window, document, Vue, location: window.location,
    ResizeObserver, getComputedStyle: window.getComputedStyle, URLSearchParams,
    setTimeout, clearTimeout, console,
    fetch: async () => {throw new Error('Network forbidden in shell unit tests');},
  });
  vm.runInContext(componentsSource.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  context.registerGlobalComponents(app);

  function mount(name, input = {}, refs = {}) {
    const definition = components.get(name), props = {...input}, events = [];
    for (const [key, spec] of Object.entries(definition.props || {})) {
      if (props[key] === undefined && spec && 'default' in Object(spec)) props[key] = spec.default;
    }
    const lifecycle = {mounted: [], unmounted: [], watchers: []};
    current = lifecycle;
    const emit = (...args) => events.push(args);
    let state;
    if (definition.setup) state = definition.setup(props, {emit, attrs: {}, slots: {}});
    else {
      // Existing Options API modal is exercised in RED as well.
      state = {...props, ...definition.data?.(), $refs: refs, $emit: emit, $nextTick: Vue.nextTick};
      for (const [key, fn] of Object.entries(definition.methods || {})) state[key] = fn.bind(state);
      if (definition.mounted) lifecycle.mounted.push(definition.mounted.bind(state));
      if (definition.beforeUnmount) lifecycle.unmounted.push(definition.beforeUnmount.bind(state));
      for (const [key, fn] of Object.entries(definition.watch || {})) {
        lifecycle.watchers.push({getter: () => props[key], value: props[key], fn: fn.bind(state)});
      }
    }
    for (const [key, value] of Object.entries(refs)) if (state[key]) state[key].value = value;
    return {state, props, definition, events,
      async start() {for (const fn of lifecycle.mounted) fn(); await flush();},
      update(values) {
        Object.assign(props, values);
        if (!definition.setup) Object.assign(state, values);
        for (const watcher of lifecycle.watchers) {
          const next = watcher.getter();
          if (next !== watcher.value) {watcher.value = next; watcher.fn(next);}
        }
      },
      unmount() {for (const fn of lifecycle.unmounted) fn(); lifecycle.watchers = [];},
      backdrop() {
        const expression = definition.template.match(/@click(?:\.self)?="([^"]+)"/)[1];
        if (typeof state[expression] === 'function') state[expression]();
        else vm.runInContext('(function (state) { with (state) { ' + expression + '; } })', context)(state);
      },
    };
  }
  function modal(input = {}) {
    const dialog = document.body.append(element('dialog'));
    const first = dialog.append(element('close', {tabIndex: 0}));
    const disabled = dialog.append(element('disabled', {tabIndex: 0, disabled: true}));
    const hidden = dialog.append(element('hidden', {tabIndex: 0, hidden: true}));
    const last = dialog.append(element('footer action', {tabIndex: 0}));
    return Object.assign(mount('app-modal', {title: 'Example', isOpen: true, ...input}, {dialog}),
      {dialog, first, last, disabled, hidden});
  }
  return {document, window, components, mount, modal, media, observations, element, opener, appRoot,
    context, app, order, loadBase() {
      for (const [, script] of baseSource.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
        context.tailwind = {};
        vm.runInContext(script.replace(/\{\[ task_labels \| tojson \]\}/g, '{}'), context);
      }
    },
  };
}

test('mobile navigation: exactly four destinations plus More, with authors/series', () => {
  const h = harness(), mobile = h.mount('app-mobile-bar', {currentPath: '/dashboard'}).state;
  assert.deepEqual(Array.from(mobile.mobileItems, item => [item.path, item.label]), [
    ['/dashboard', '首页'], ['/dashboard/novels', '书库'],
    ['/dashboard/follows', '作者与系列'], ['/dashboard/logs', '任务'],
  ]);
  assert.deepEqual(Array.from(mobile.moreItems, item => item.path), [
    '/dashboard/preferences', '/dashboard/pending-deletions', '/dashboard/settings',
  ]);
  assert.equal(mobile.moreItems.at(-1).children.length, 4, 'all settings pages remain reachable');
});

test('navigation: detail routes stay in their groups without prefix collisions', () => {
  for (const [currentPath, path] of [
    ['/dashboard/novels/12', '/dashboard/novels'],
    ['/dashboard/users/12', '/dashboard/follows'],
    ['/dashboard/series/12', '/dashboard/follows'],
    ['/dashboard/settings/agents', '/dashboard/settings'],
  ]) {
    const h = harness(), sidebar = h.mount('app-sidebar-nav', {currentPath}).state;
    assert.deepEqual(Array.from(sidebar.items.filter(item => !item.type && sidebar.isActive(item)), item => item.path), [path]);
  }
  const h = harness(), mobile = h.mount('app-mobile-bar', {currentPath: '/dashboard/novels-unrelated'}).state;
  assert.equal(mobile.mobileItems.some(mobile.isActive), false);
});

test('More: active group, open/close, pathname and desktop transitions', async () => {
  const h = harness(), mobile = h.mount('app-mobile-bar', {currentPath: '/dashboard/settings/models'}, {bar: h.element('bar')});
  await mobile.start();
  assert.equal(mobile.state.moreActive.value, true);
  mobile.state.moreOpen.value = true;
  mobile.state.closeMore();
  assert.equal(mobile.state.moreOpen.value, false);
  mobile.state.moreOpen.value = true;
  mobile.update({currentPath: '/dashboard'});
  assert.equal(mobile.state.moreOpen.value, false);
  assert.equal(mobile.state.moreActive.value, false);
  mobile.state.moreOpen.value = true;
  h.window.dispatch('pagehide');
  assert.equal(mobile.state.moreOpen.value, false, 'bfcache return must not reopen More');
  mobile.state.moreOpen.value = true;
  h.media.matches = false;
  h.media.dispatch('change', {matches: false});
  assert.equal(mobile.state.moreOpen.value, false, 'hidden mobile popup must not strand desktop navigation');
  mobile.unmount();
  assert.equal(h.media.listenerCount(), 0);
  assert.equal(h.window.listenerCount(), 0);
});

test('More: measured bar clearance and observer cleanup, including hidden reader bar', async () => {
  const h = harness(), css = h.document.documentElement.style;
  css.setProperty('--mobile-bar-height', '70px');
  const bar = h.element('bar');
  const mobile = h.mount('app-mobile-bar', {currentPath: '/dashboard'}, {bar});
  await mobile.start();
  assert.equal(css.getPropertyValue('--mobile-bar-height'), '84px');
  bar.getBoundingClientRect = () => ({height: 0});
  h.observations[0].callback([{target: bar}]);
  assert.equal(css.getPropertyValue('--mobile-bar-height'), '84px', 'hidden bar must not erase fallback clearance');
  mobile.unmount();
  assert.equal(css.getPropertyValue('--mobile-bar-height'), '70px');
  assert.ok(h.observations.every(observer => observer.disconnected));
});

test('pagination: emit only changed, finite, integer pages within range', () => {
  const h = harness(), pagination = h.mount('app-pagination', {page: 2, pages: 4});
  for (const page of [0, -1, 5, 2, 1.5, NaN, Infinity]) pagination.state.go(page);
  assert.deepEqual(pagination.events, []);
  pagination.state.go(1); pagination.state.go(3);
  assert.deepEqual(pagination.events, [['update:page', 1], ['update:page', 3]]);
});

test('initVueApp: optional page registration runs after globals and before mount', () => {
  const h = harness();
  h.loadBase(); h.order.length = 0;
  const options = {setup() {return {};}};
  h.window.registerPageComponents = app => {assert.equal(app, h.app); h.order.push('page hook');};
  assert.equal(h.window.initVueApp(options), h.app);
  assert.equal(h.app.options, options);
  assert.ok(h.order.indexOf('app-modal') < h.order.indexOf('page hook'));
  assert.deepEqual(h.order.slice(-2), ['page hook', ['mount', '#app']]);
  assert.equal(h.app.config.globalProperties.formatDbTime, h.window.formatDbTime);
  assert.equal(h.app.config.globalProperties.toast, h.window.toast);
  delete h.window.registerPageComponents;
  assert.doesNotThrow(() => h.window.initVueApp(options), 'legacy pages do not need the hook');
});

for (const name of ['app-sidebar-footer', 'app-mobile-bar']) {
  for (const [label, response, message] of [
    ['HTTP error', () => json({detail: 'store unavailable'}, false), 'store unavailable'],
    ['business error', () => json({ok: false, error: 'revocation failed'}), 'revocation failed'],
    ['network error', () => {throw new Error('offline');}, 'offline'],
    ['unconfirmed response', () => json({}), '退出'],
    ['invalid JSON', () => ({ok: true, status: 200, json: async () => {throw new Error('bad JSON');}}), '退出'],
  ]) {
    test(`${name}: logout ${label} stays put and exposes retryable feedback`, async () => {
      const h = harness(), mounted = h.mount(name, {currentPath: '/dashboard'});
      h.window.csrfFetch = async (url, options) => {
        assert.equal(url, '/api/auth/logout'); assert.equal(options.method, 'POST'); return response();
      };
      await mounted.state.logout();
      assert.equal(h.window.location.href, '/dashboard');
      assert.ok(mounted.state.logoutError.value.includes(message));
      assert.equal(mounted.state.loggingOut.value, false);
      h.window.csrfFetch = async () => json({ok: true});
      await mounted.state.logout();
      assert.equal(h.window.location.href, '/api/auth/login');
      assert.equal(mounted.state.logoutError.value, '');
    });
  }
}

test('logout: a request pending across desktop/mobile cannot be submitted twice', async () => {
  const h = harness();
  const footer = h.mount('app-sidebar-footer'), mobile = h.mount('app-mobile-bar', {currentPath: '/dashboard'});
  let resolve, requests = 0;
  h.window.csrfFetch = () => {requests++; return new Promise(done => {resolve = done;});};
  const first = footer.state.logout();
  await mobile.state.logout();
  assert.equal(requests, 1);
  assert.equal(mobile.state.loggingOut.value, true);
  resolve(json({ok: true})); await first;
  assert.equal(h.window.location.href, '/api/auth/login');
});

test('modal: closeOnBackdrop defaults true; false blocks only backdrop, not Escape', async () => {
  const h = harness();
  assert.equal(h.components.get('app-modal').props.closeOnBackdrop?.default, true);
  const modal = h.modal({closeOnBackdrop: false}); await modal.start();
  modal.backdrop(); assert.deepEqual(modal.events, []);
  const escape = h.document.dispatch('keydown', {key: 'Escape'});
  assert.equal(escape.defaultPrevented, true);
  assert.deepEqual(modal.events, [['close']]);
  modal.update({closeOnBackdrop: true}); modal.backdrop();
  assert.deepEqual(modal.events, [['close'], ['close']]);
  modal.unmount();
});

test('modal: initially open locks scrolling, contains focus, and restores exact styles/focus', async () => {
  const h = harness();
  h.document.body.style.setProperty('overflow', 'auto', 'important');
  h.document.body.style.setProperty('padding-right', '5px');
  h.document.documentElement.style.setProperty('overflow', 'scroll');
  const modal = h.modal(); await modal.start();
  assertFocus(h, modal.dialog);
  assert.equal(h.document.body.style.overflow, 'hidden');
  assert.equal(h.document.documentElement.style.overflow, 'hidden');
  assert.equal(h.appRoot.inert, true);
  modal.update({isOpen: false}); await flush();
  assertFocus(h, h.opener);
  assert.equal(h.document.body.style.overflow, 'auto');
  assert.equal(h.document.body.style.getPropertyPriority('overflow'), 'important');
  assert.equal(h.document.body.style.getPropertyValue('padding-right'), '5px');
  assert.equal(h.document.documentElement.style.overflow, 'scroll');
  assert.equal(h.appRoot.inert, false);
  assert.equal(h.document.listenerCount(), 0);
});

test('modal: Tab/Shift+Tab cycle enabled visible controls and pull escaped focus back', async () => {
  const h = harness(), modal = h.modal(); await modal.start();
  h.document.dispatch('keydown', {key: 'Tab'});
  assertFocus(h, modal.first);
  modal.last.focus();
  assert.equal(h.document.dispatch('keydown', {key: 'Tab'}).defaultPrevented, true);
  assertFocus(h, modal.first);
  h.document.dispatch('keydown', {key: 'Tab', shiftKey: true});
  assertFocus(h, modal.last);
  const outside = h.document.body.append(h.element('outside', {tabIndex: 0}));
  outside.focus();
  assert.ok(modal.dialog.contains(h.document.activeElement));
  modal.first.disabled = true; modal.last.hidden = true;
  h.document.dispatch('keydown', {key: 'Tab'});
  assertFocus(h, modal.dialog);
  modal.unmount();
});

test('modal: stacked dialogs close only the top and retain the scroll lock', async () => {
  const h = harness(), first = h.modal(); await first.start(); first.last.focus();
  const second = h.modal(); await second.start();
  assert.ok(second.state.layer.value > first.state.layer.value);
  assert.equal(first.dialog.inert, true);
  first.backdrop(); assert.deepEqual(first.events, []);
  h.document.dispatch('keydown', {key: 'Escape'});
  assert.deepEqual(first.events, []); assert.deepEqual(second.events, [['close']]);
  second.update({isOpen: false}); await flush();
  assert.equal(h.document.body.style.overflow, 'hidden');
  assertFocus(h, first.last);
  assert.equal(first.dialog.inert, false);
  first.unmount();
  assert.equal(h.document.body.style.overflow, '');
  assertFocus(h, h.opener);
  assert.equal(h.document.listenerCount(), 0);
});

test('modal: removing a lower dialog preserves top focus and repairs the return chain', async () => {
  const h = harness(), first = h.modal(); await first.start(); first.last.focus();
  const second = h.modal(); await second.start();
  first.unmount(); first.dialog.isConnected = false; first.last.isConnected = false;
  assertFocus(h, second.dialog);
  assert.equal(h.document.body.style.overflow, 'hidden');
  second.unmount();
  assertFocus(h, h.opener);
  assert.equal(h.document.body.style.overflow, '');
  assert.equal(h.document.listenerCount(), 0);
});

test('modal: closed-on-mount, rapid changes, and unmount cannot leave stale locks or focus', async () => {
  const h = harness(), modal = h.modal({isOpen: false}); await modal.start();
  assert.equal(h.document.body.style.overflow, '');
  assertFocus(h, h.opener);
  modal.update({isOpen: true}); modal.update({isOpen: false}); await flush();
  assert.equal(h.document.body.style.overflow, '');
  assertFocus(h, h.opener);
  modal.update({isOpen: true}); modal.unmount(); await flush();
  assert.equal(h.document.body.style.overflow, '');
  assert.equal(h.document.listenerCount(), 0);
});

test('modal: reopen captures the current trigger and detached triggers are safe', async () => {
  const h = harness(), modal = h.modal(); await modal.start();
  modal.update({isOpen: false}); await flush();
  const next = h.appRoot.append(h.element('next trigger', {tabIndex: 0})); next.focus();
  modal.update({isOpen: true}); await flush();
  modal.update({isOpen: false}); await flush();
  assertFocus(h, next);
  modal.update({isOpen: true}); await flush(); next.isConnected = false;
  assert.doesNotThrow(() => modal.unmount());
  assert.equal(h.document.body.style.overflow, '');
});

test('modal: positive tabindex uses browser tab order at both trap boundaries', async () => {
  const h = harness(), modal = h.modal();
  modal.dialog.append(h.element('second in tab order', {tabIndex: 2}));
  const first = modal.dialog.append(h.element('first in tab order', {tabIndex: 1}));
  await modal.start();
  h.document.dispatch('keydown', {key: 'Tab'});
  assertFocus(h, first);
  h.document.dispatch('keydown', {key: 'Tab', shiftKey: true});
  assertFocus(h, modal.last);
  h.document.dispatch('keydown', {key: 'Tab'});
  assertFocus(h, first);
  modal.unmount();
});

test('modal: composing an IME candidate must not close the dialog on Escape', async () => {
  const h = harness(), modal = h.modal(); await modal.start();
  const composing = h.document.dispatch('keydown', {key: 'Escape', isComposing: true});
  assert.deepEqual(modal.events, []);
  assert.notEqual(composing.defaultPrevented, true);
  h.document.dispatch('keydown', {key: 'Escape', isComposing: false});
  assert.deepEqual(modal.events, [['close']]);
  modal.unmount();
});

test('modal: moves focus before hiding the previous dialog from assistive technology', async () => {
  const h = harness(), first = h.modal(); await first.start(); first.last.focus();
  const setAttribute = first.dialog.setAttribute;
  let hidFocusedDescendant = false;
  first.dialog.setAttribute = (key, value) => {
    if (key === 'aria-hidden' && value === 'true') {
      hidFocusedDescendant = first.dialog.contains(h.document.activeElement);
    }
    setAttribute(key, value);
  };
  const second = h.modal(); await second.start();
  assert.equal(hidFocusedDescendant, false, 'Do not hide a focused descendant from AT');
  assertFocus(h, second.dialog);
  second.unmount(); first.unmount();
});
