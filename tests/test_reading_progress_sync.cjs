const test = require('node:test');
const assert = require('node:assert/strict');
const {create, validateRecord} = require('../src/pixiv_novel_sync/static/js/reading_progress_sync.js');

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
const response = (data, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => data});
const record = (version = 0, progress = 0, is_reset = false) => ({novel_id: 1, version, progress, is_reset,
  status: progress === 100 ? 'completed' : progress ? 'reading' : 'unread', last_read_at: null, updated_at: null});
const storage = map => ({get length() { return map.size; }, key: index => [...map.keys()][index],
  getItem: key => map.get(key) ?? null, setItem: (key, value) => map.set(key, String(value)), removeItem: key => map.delete(key)});
let identity = 0;
const uuid = () => `00000000-0000-4000-8000-${String(++identity).padStart(12, '0')}`;

function server(initial = record()) {
  const receipts = new Map();
  let current = initial;
  return {
    get record() { return current; },
    set record(value) { current = value; },
    request(_url, options) {
      if (options.method === 'GET') return Promise.resolve(response(current));
      const body = JSON.parse(options.body), previous = receipts.get(body.mutation_id);
      if (previous) {
        if (previous.body !== options.body || previous.method !== options.method) return Promise.resolve(response({error: 'progress_idempotency_conflict', record: current}, 409));
        return Promise.resolve(response({ok: true, success: true, record: current, applied_version: previous.version, replayed: true}));
      }
      if (body.base_version !== current.version) return Promise.resolve(response({error: 'progress_conflict', record: current}, 409));
      current = record(current.version + 1, options.method === 'DELETE' ? 0 : body.progress, options.method === 'DELETE');
      receipts.set(body.mutation_id, {version: current.version, body: options.body, method: options.method});
      return Promise.resolve(response({ok: true, success: true, record: current, applied_version: current.version, replayed: false}));
    },
  };
}

function fixture({remote = server(), local = new Map(), request, clock = {now: 10000}} = {}) {
  const calls = [], timers = new Map(), changes = [], commits = [];
  let nextTimer = 0;
  const engine = create({novelId: 1, storage: storage(local), uuid, now: () => clock.now,
    setTimeout: (fn, delay) => { const id = ++nextTimer; timers.set(id, {fn, due: clock.now + delay}); return id; },
    clearTimeout: id => timers.delete(id),
    request: async (url, options) => { calls.push({url, options: structuredClone(options)}); return (request || remote.request)(url, options); },
    onChange: state => changes.push(state), onCommit: receipt => commits.push(receipt),
    errorText: data => data.detail || data.error || 'request failed',
  });
  const api = {engine, remote, local, calls, timers, changes, commits, clock,
    writes: () => calls.filter(call => call.options.method !== 'GET'),
    async tick(ms = 5000) {
      clock.now += ms;
      for (const [id, timer] of [...timers]) if (timer.due <= clock.now) { timers.delete(id); timer.fn(); }
      for (let i = 0; i < 25; i++) await Promise.resolve();
    },
  };
  return api;
}

test('strict server record rejects missing versions, booleans, another novel, and inconsistent reset', () => {
  for (const bad of [{progress: 60}, {...record(), version: true}, {...record(), novel_id: 2}, record(3, 40, true),
    {...record(), is_reset: undefined}, {...record(), updated_at: 32}]) assert.throws(() => validateRecord(bad, 1));
  assert.deepEqual(validateRecord(record(4, 0, true), 1), record(4, 0, true));
});

test('automatic save coalesces scroll for five seconds and no load creates a zero mutation', async () => {
  const f = fixture();
  assert.equal((await f.engine.refresh()).restore, true);
  await f.tick(5000);
  assert.equal(f.writes().length, 0);
  f.engine.sample(10);
  f.engine.sample(23);
  await f.tick(0);
  assert.equal(f.writes().length, 1);
  assert.equal(JSON.parse(f.writes()[0].options.body).progress, 23);
  f.engine.sample(34);
  await f.tick(4999);
  assert.equal(f.writes().length, 1);
  await f.tick(1);
  assert.equal(f.writes().length, 2);
  assert.equal(JSON.parse(f.writes()[1].options.body).base_version, 1);
  f.engine.dispose();
});

test('user scroll while a write is in flight survives its acknowledgment with the dependent version', async () => {
  const network = deferred(), remote = server(record(4, 20));
  let wait = true;
  const f = fixture({remote, request: async (url, opts) => opts.method !== 'GET' && wait ? network.promise : remote.request(url, opts)});
  await f.engine.refresh();
  const saving = f.engine.save(31);
  const sent = f.writes()[0];
  f.engine.sample(52);
  assert.equal(f.engine.state().dirty.base_version, 5);
  wait = false;
  network.resolve(await remote.request(sent.url, sent.options));
  await saving;
  assert.equal(remote.record.progress, 52);
  assert.equal(remote.record.version, 6);
  assert.equal(f.writes().length, 2);
  assert.equal(f.engine.state().hasPending, false);
  f.engine.dispose();
});

test('offline restart retries immutable mutation before handling a newer local sample', async () => {
  const local = new Map(), remote = server(record(8, 15));
  let offline = true;
  const f = fixture({local, remote, request: (url, opts) => {
    if (opts.method !== 'GET' && offline) throw Error('offline');
    return remote.request(url, opts);
  }});
  await f.engine.refresh();
  await f.engine.save(33);
  const body = f.writes()[0].options.body;
  f.engine.sample(45);
  f.engine.dispose();
  offline = false;
  const next = fixture({local, remote});
  await next.engine.refresh();
  await next.engine.flush({explicit: true});
  assert.equal(next.writes()[0].options.body, body);
  assert.equal(JSON.parse(next.writes()[1].options.body).base_version, 9);
  assert.equal(remote.record.version, 10);
  assert.equal(remote.record.progress, 45);
  assert.equal([...local.keys()].filter(key => key.startsWith('pixivNovelSync:reading-sync:')).length, 0);
  next.engine.dispose();
});

test('lost response replay carrying a newer current record cannot rebase later dirty intent', async () => {
  const remote = server(record(4, 20));
  let drop = true;
  const f = fixture({remote, request: async (url, opts) => {
    const result = await remote.request(url, opts);
    if (opts.method !== 'GET' && drop) { drop = false; throw Error('response lost'); }
    return result;
  }});
  await f.engine.refresh();
  await f.engine.save(30);
  f.engine.sample(50);
  remote.record = record(6, 85);
  await f.engine.refresh();
  await f.engine.flush({explicit: true});
  assert.equal(f.engine.state().record.version, 6);
  assert.equal(f.engine.state().dirty.base_version, 5);
  assert.equal(f.engine.state().phase, 'conflict');
  assert.equal(f.writes().length, 2);
  assert.equal(remote.record.progress, 85);
  await f.engine.keepLocal();
  assert.equal(remote.record.version, 7);
  assert.equal(remote.record.progress, 50);
  f.engine.dispose();
});

test('late GET cannot regress a successful write or obtain authority for dirty data', async () => {
  const remote = server(record(1, 10)), get = deferred();
  let delayed = false;
  const f = fixture({remote, request: (url, opts) => opts.method === 'GET' && delayed ? get.promise : remote.request(url, opts)});
  await f.engine.refresh();
  delayed = true;
  const refreshing = f.engine.refresh();
  await f.engine.save(25);
  get.resolve(response(record(1, 10)));
  await refreshing;
  assert.equal(f.engine.state().record.version, 2);
  assert.equal(f.engine.state().record.progress, 25);
  assert.equal(f.engine.state().phase, 'synced');
  f.engine.dispose();
});

test('scroll before first version check conflicts with an existing server record and never writes it blindly', async () => {
  const get = deferred(), f = fixture({request: () => get.promise});
  const loading = f.engine.refresh();
  f.engine.sample(7);
  get.resolve(response(record(9, 67)));
  const result = await loading;
  await f.engine.flush();
  assert.equal(result.restore, false);
  assert.equal(f.engine.state().phase, 'conflict');
  assert.equal(f.engine.state().dirty.base_version, null);
  assert.equal(f.writes().length, 0);
  f.engine.dispose();
});

test('scroll before first GET is allowed at virgin version zero only', async () => {
  const remote = server(), get = deferred();
  let first = true;
  const f = fixture({remote, request: (url, opts) => opts.method === 'GET' && first ? get.promise : remote.request(url, opts)});
  const loading = f.engine.refresh();
  f.engine.sample(12);
  first = false;
  get.resolve(response(record()));
  await loading;
  await f.engine.flush();
  assert.equal(remote.record.progress, 12);
  assert.equal(remote.record.version, 1);
  f.engine.dispose();
});

test('old-server response with no version blocks automatic, explicit and keepalive writes', async () => {
  const f = fixture({request: () => response({progress: 60})});
  await f.engine.refresh();
  f.engine.sample(15);
  await f.engine.flush({keepalive: true, explicit: true});
  assert.equal(f.engine.state().phase, 'unsupported');
  assert.equal(f.writes().length, 0);
  f.engine.dispose();
});

test('cross-tab reset tombstone prevents old offline position from reviving the record', async () => {
  const remote = server(record(3, 48)), shared = new Map();
  const oldTab = fixture({remote, local: shared}), newTab = fixture({remote, local: shared});
  await oldTab.engine.refresh();
  await newTab.engine.refresh();
  oldTab.engine.sample(65);
  await newTab.engine.reset();
  await oldTab.engine.flush();
  assert.equal(remote.record.version, 4);
  assert.equal(remote.record.is_reset, true);
  assert.equal(oldTab.engine.state().phase, 'conflict');
  assert.equal(oldTab.engine.state().local.progress, 65);
  assert.equal(oldTab.engine.useServer().is_reset, true);
  assert.equal(oldTab.engine.state().hasPending, false);
  oldTab.engine.dispose(); newTab.engine.dispose();
});

test('cross-tab notification with a newer server version keeps view authority old until an explicit choice', async () => {
  const remote = server(record(2, 42)), f = fixture({remote});
  await f.engine.refresh();
  remote.record = record(3, 0, true);
  await f.engine.storageChanged({key: 'pixivNovelSync:reading-server:1'});
  assert.equal(f.engine.state().phase, 'conflict');
  f.engine.sample(55);
  assert.equal(f.engine.state().dirty.base_version, 2);
  await f.engine.flush();
  assert.equal(f.writes().length, 0);
  await f.engine.keepLocal();
  assert.equal(remote.record.progress, 55);
  assert.equal(remote.record.version, 4);
  f.engine.dispose();
});

test('explicit local choice still conflicts if another device wins its next CAS', async () => {
  const remote = server(record(3, 30)), f = fixture({remote});
  await f.engine.refresh();
  f.engine.sample(40);
  remote.record = record(4, 75);
  await f.engine.flush();
  remote.record = record(5, 90);
  await f.engine.keepLocal();
  assert.equal(f.engine.state().phase, 'conflict');
  assert.equal(JSON.parse(f.writes()[1].options.body).base_version, 4);
  assert.equal(remote.record.progress, 90);
  f.engine.dispose();
});

test('pagehide flush preserves exact pending body and carries keepalive without beacon', async () => {
  const remote = server(record(1, 10));
  let failure = true;
  const f = fixture({remote, request: (url, opts) => {
    if (opts.method !== 'GET' && failure) throw Error('offline');
    return remote.request(url, opts);
  }});
  await f.engine.refresh();
  await f.engine.save(28);
  failure = false;
  await f.engine.flush({keepalive: true});
  assert.equal(f.writes()[0].options.body, f.writes()[1].options.body);
  assert.equal(f.writes()[1].options.keepalive, true);
  assert.deepEqual(f.writes()[1].options.headers, {'Content-Type': 'application/json'});
  f.engine.dispose();
});

test('separate tab journals cannot erase a newer sample from the originating tab', async () => {
  const remote = server(record(1, 12)), shared = new Map();
  const a = fixture({remote, local: shared});
  await a.engine.refresh();
  a.engine.sample(30);
  const originalKey = [...shared.keys()].find(key => key.includes(':reading-sync:'));
  const b = fixture({remote, local: shared});
  a.engine.sample(55);
  const newerKey = [...shared.keys()].find(key => key.includes(':reading-sync:'));
  assert.notEqual(newerKey, originalKey, 'new samples must use immutable new snapshot keys');
  const newer = shared.get(newerKey);
  await b.engine.refresh();
  await b.engine.flush();
  assert.equal(shared.get(newerKey), newer, 'recovery may only remove immutable previously observed journals');
  await a.engine.flush();
  assert.equal(a.engine.state().phase, 'conflict');
  assert.equal(a.engine.state().local.progress, 55);
  a.engine.dispose(); b.engine.dispose();
});

test('401 retains the original mutation for retry and never reports a save success', async () => {
  const remote = server(record(2, 9));
  let unauthorized = true;
  const f = fixture({remote, request: (url, opts) => opts.method !== 'GET' && unauthorized
    ? response({error: 'authentication_required'}, 401) : remote.request(url, opts)});
  await f.engine.refresh();
  const result = await f.engine.save(16);
  assert.ok(result.error);
  assert.equal(f.engine.state().phase, 'auth');
  const before = f.writes()[0].options.body;
  await f.tick(30000);
  assert.equal(f.writes().length, 1);
  unauthorized = false;
  await f.engine.refresh();
  await f.engine.flush({explicit: true});
  assert.equal(f.writes()[1].options.body, before);
  assert.equal(remote.record.progress, 16);
  f.engine.dispose();
});

test('storage failures are visible rather than falsely claiming durable offline saving', async () => {
  const remote = server(record(1, 12)), f = fixture({remote});
  await f.engine.refresh();
  f.engine.dispose();
  const broken = create({novelId: 1, uuid, storage: {length: 0, getItem() { return null; },
    setItem() { throw Error('quota'); }, removeItem() { throw Error('blocked'); }},
  request: async (_url, opts) => opts.method === 'GET' ? response(record(1, 12)) : Promise.reject(Error('offline'))});
  await broken.refresh();
  await broken.save(22);
  assert.equal(broken.state().persistenceError, true);
  assert.equal(broken.state().hasPending, true);
  broken.dispose();
});

test('independent dirty tabs do not turn journal storage events into an HTTP feedback loop', async () => {
  const remote = server(record(1, 12)), shared = new Map();
  const a = fixture({remote, local: shared}), b = fixture({remote, local: shared});
  await a.engine.refresh();
  await b.engine.refresh();
  a.engine.sample(20);
  b.engine.sample(30);
  const keys = [...shared.keys()].filter(key => key.includes(':reading-sync:'));
  const before = new Map(shared);
  for (let index = 0; index < 100; index++) {
    await a.engine.storageChanged({key: keys[1]});
    await b.engine.storageChanged({key: keys[0]});
  }
  assert.equal(a.calls.length, 1);
  assert.equal(b.calls.length, 1);
  assert.deepEqual(shared, before);
  a.engine.dispose(); b.engine.dispose();
});

test('repeated offline reload coalesces the exact same pending mutation into one durable journal', async () => {
  const remote = server(record(2, 15)), shared = new Map();
  const request = (url, opts) => opts.method === 'GET' ? remote.request(url, opts) : Promise.reject(Error('offline'));
  let f = fixture({remote, local: shared, request});
  await f.engine.refresh();
  await f.engine.save(35);
  const original = f.engine.state().pending;
  for (let index = 0; index < 50; index++) {
    f.engine.dispose();
    f = fixture({remote, local: shared, request});
    await f.engine.refresh();
    await f.engine.flush({keepalive: true});
    assert.deepEqual(f.engine.state().pending, original);
    assert.equal([...shared.keys()].filter(key => key.includes(':reading-sync:')).length, 1);
  }
  f.engine.dispose();
});
