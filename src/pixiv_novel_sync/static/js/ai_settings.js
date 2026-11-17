(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AISettings = api;
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const copy = value => JSON.parse(JSON.stringify(value));
  const has = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  const views = new Set(['providers', 'models', 'groups']);
  function optionKey(o) {
    if (o.kind === 'agent') return JSON.stringify(['agent', o.agent_id]);
    return JSON.stringify(o.kind === 'pool' ? ['pool', o.pool_id]
      : ['model', o.provider_id, o.model_key]);
  }
  function retainCurrent(options, current) {
    const rows = options.map(o => ({...o, current: false}));
    if (!current) return rows;
    const key = optionKey(current), at = rows.findIndex(o => optionKey(o) === key);
    const row = at < 0 ? {...current, availability: current.availability || 'unavailable'} : rows.splice(at, 1)[0];
    return [{...row, current: true}, ...rows];
  }
  function filterOptions(options, query) {
    const words = String(query || '').toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    return options.filter(o => o.current || words.every(word =>
      [o.label, o.model_key, o.provider_name, o.provider_id, o.pool_id, o.agent_id, o.availability, o.reason]
        .map(x => String(x ?? '')).join(' ').toLocaleLowerCase().includes(word)));
  }
  function readWorkspace(search) {
    const p = new URLSearchParams(search), raw = p.get('provider');
    const number = raw !== null && /^[1-9]\d*$/.test(raw) ? Number(raw) : null;
    const valid = number !== null && Number.isSafeInteger(number);
    return {view: views.has(p.get('view')) ? p.get('view') : 'providers',
      providerId: valid ? number : null, invalidProvider: raw !== null && !valid ? raw : null};
  }
  function workspaceSearch(search, patch) {
    const p = new URLSearchParams(search);
    if (has(patch, 'view')) p.set('view', views.has(patch.view) ? patch.view : 'providers');
    if (has(patch, 'providerId')) {
      if (patch.providerId === null) p.delete('provider');
      else if (Number.isSafeInteger(patch.providerId) && patch.providerId > 0) p.set('provider', String(patch.providerId));
      else throw new TypeError('providerId must be a positive integer or null');
    }
    const value = p.toString();
    return value ? '?' + value : '';
  }
  function touchedPatch(original, draft, fields) {
    const patch = {};
    for (const key of new Set(fields)) {
      if (has(draft, key) && draft[key] !== undefined && JSON.stringify(draft[key]) !== JSON.stringify(original[key])) patch[key] = copy(draft[key]);
    }
    return patch;
  }
  function moveItem(items, from, to) {
    const result = items.slice();
    if (!Number.isInteger(from) || !Number.isInteger(to) || from < 0 || to < 0 || from >= result.length || to >= result.length) return result;
    result.splice(to, 0, result.splice(from, 1)[0]);
    return result;
  }
  function initialModelSelection(catalogue, selectedKeys) {
    if (!catalogue || typeof catalogue.catalog_revision !== 'string' || !/^[0-9a-f]{64}$/.test(catalogue.catalog_revision) || !Array.isArray(catalogue.items)) throw new TypeError('A complete catalogue revision is required');
    const wanted = new Set(selectedKeys);
    const discovered = catalogue.items.filter(row => row.discovered === true && row.discovered_available === true);
    const keys = new Set(discovered.map(row => row.model_key));
    const changes = discovered.map(row => ({id: row.id, enabled: wanted.has(row.model_key)}));
    if (changes.length > 5000) throw new RangeError('Atomic model selection exceeds 5000 rows');
    return {revision: catalogue.catalog_revision, changes, missing: [...wanted].filter(key => !keys.has(key))};
  }
  function parseNullableOutput(value) {
    if (value === null) return null;
    const n = typeof value === 'string' && /^[1-9]\d*$/.test(value) ? Number(value) : value;
    if (typeof n !== 'number' || !Number.isInteger(n) || n < 1 || n > 1000000) throw new TypeError('Output tokens must be an integer from 1 to 1000000, or an explicit clear');
    return n;
  }
  function createDraftGuard({read, save, discard, confirm}) {
    let baseline = JSON.stringify(read()), pending = null;
    const isDirty = () => JSON.stringify(read()) !== baseline;
    const markSaved = () => { baseline = JSON.stringify(read()); };
    function leave() {
      if (!isDirty()) return Promise.resolve(true);
      if (pending) return pending;
      pending = (async () => {
        const action = await confirm();
        if (action === 'save') {
          if (await save() !== true) return false;
          markSaved();
          return true;
        }
        if (action === 'discard') { await discard(); markSaved(); return true; }
        return false;
      })().finally(() => { pending = null; });
      return pending;
    }
    return {isDirty, markSaved, leave};
  }
  return {optionKey, retainCurrent, filterOptions, readWorkspace, workspaceSearch,
    touchedPatch, moveItem, initialModelSelection, parseNullableOutput, createDraftGuard};
});
