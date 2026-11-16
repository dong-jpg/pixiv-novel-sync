(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ReadingProgressSync = api;
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';

  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const STATUSES = new Set(['unread', 'reading', 'completed']);
  const copy = value => value == null ? value : JSON.parse(JSON.stringify(value));
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  const progressStatus = progress => progress >= 100 ? 'completed' : (progress > 0 ? 'reading' : 'unread');

  function validateRecord(value, novelId) {
    if (!value || typeof value !== 'object' || value.novel_id !== novelId || !integer(value.version)
      || !integer(value.progress) || value.progress > 100 || !STATUSES.has(value.status)
      || typeof value.is_reset !== 'boolean'
      || !['last_read_at', 'updated_at'].every(key => value[key] === null || typeof value[key] === 'string')
      || (value.is_reset && (value.progress !== 0 || value.status !== 'unread'))) {
      throw new Error('服务器尚不支持可靠的阅读进度同步，请刷新或稍后重试。');
    }
    return Object.fromEntries(['novel_id', 'progress', 'status', 'version', 'is_reset', 'last_read_at', 'updated_at']
      .map(key => [key, value[key]]));
  }

  function randomUUID() {
    const crypto = globalThis.crypto;
    if (crypto?.randomUUID) return crypto.randomUUID();
    if (!crypto?.getRandomValues) throw new Error('当前浏览器无法安全生成同步标识。');
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
  }

  function validIntent(value, pending = false) {
    if (!value || !['POST', 'DELETE'].includes(value.method)
      || !(value.base_version === null || integer(value.base_version))
      || (pending && (!integer(value.base_version) || !UUID.test(value.mutation_id)))) return false;
    if (value.method === 'POST' && (!integer(value.progress) || value.progress > 100 || !STATUSES.has(value.status))) return false;
    if (value.parent != null && !UUID.test(value.parent)) return false;
    return true;
  }

  function create(options) {
    const novelId = options.novelId;
    if (!Number.isSafeInteger(novelId) || novelId <= 0) throw new Error('无效的小说编号。');
    const now = options.now || Date.now;
    const uuid = options.uuid || randomUUID;
    const later = options.setTimeout || setTimeout;
    const cancelTimer = options.clearTimeout || clearTimeout;
    const storage = options.storage;
    const prefix = `pixivNovelSync:reading-sync:${novelId}:`;
    const cacheKey = `pixivNovelSync:reading-server:${novelId}`;
    // 日志快照不可变：新状态写新 key，跨标签页不会读改写同一条离线记录。
    let journalKey = null, journalContent = null;
    const endpoint = `/api/dashboard/novels/${encodeURIComponent(novelId)}/progress`;
    let record = null, viewVersion = null, pending = null, dirty = null, conflict = null;
    let phase = 'checking', lastError = '', persistenceError = false;
    let initialized = false, disposed = false, busy = false, timer = null;
    let reading = null, writing = null, lastSentAt = now(), ambiguousRecovery = false;
    let sources = [];

    function readStorage(key) {
      try { return storage?.getItem(key) || null; } catch { persistenceError = true; return null; }
    }
    function journals() {
      const result = [];
      try {
        for (let index = 0; storage && index < storage.length; index++) {
          const key = storage.key(index);
          if (!key?.startsWith(prefix) || key === journalKey) continue;
          const raw = storage.getItem(key);
          if (!raw || raw.length > 16384) continue;
          let value;
          try { value = JSON.parse(raw); } catch { continue; }
          if (value?.schema !== 2 || value.novel_id !== novelId
            || (value.pending && !validIntent(value.pending, true))
            || (value.dirty && !validIntent(value.dirty))
            || !(value.pending || value.dirty)) continue;
          result.push({key, raw, value});
        }
      } catch { persistenceError = true; }
      return result.sort((a, b) => (Number(b.value.updated_at) || 0) - (Number(a.value.updated_at) || 0));
    }
    try {
      const cached = readStorage(cacheKey);
      if (cached) record = validateRecord(JSON.parse(cached), novelId);
    } catch { /* 旧缓存只能丢弃，不能转换成无版本写入。 */ }
    viewVersion = record?.version ?? null;
    sources = journals();
    if (sources.length) {
      const recovered = sources[0].value;
      journalKey = sources[0].key;
      journalContent = JSON.stringify({schema: 2, novel_id: novelId, view_version: recovered.view_version,
        pending: recovered.pending, dirty: recovered.dirty});
      pending = copy(recovered.pending);
      dirty = copy(recovered.dirty);
      ambiguousRecovery = new Set(sources.map(entry => JSON.stringify([entry.value.pending, entry.value.dirty]))).size > 1;
      if (integer(recovered.view_version)) viewVersion = recovered.view_version;
      phase = 'pending';
    } else if (options.legacyProgress !== undefined && integer(options.legacyProgress) && options.legacyProgress <= 100) {
      dirty = {method: 'POST', base_version: null, legacy: true, progress: options.legacyProgress,
        status: progressStatus(options.legacyProgress)};
    }

    function localIntent() { return dirty || pending; }
    function state() {
      const local = localIntent();
      return copy({phase, record, pending, dirty, conflict, busy, initialized, lastError, persistenceError,
        hasPending: !!(pending || dirty),
        local: local ? {progress: local.method === 'DELETE' ? 0 : local.progress,
          status: local.method === 'DELETE' ? 'unread' : local.status, is_reset: local.method === 'DELETE'} : null});
    }
    function notify() { if (!disposed) options.onChange?.(state()); }
    function persist() {
      try {
        if (pending || dirty) {
          const content = {schema: 2, novel_id: novelId, view_version: viewVersion, pending, dirty};
          const serialized = JSON.stringify(content);
          if (serialized !== journalContent || !journalKey || !storage.getItem(journalKey)) {
            const nextKey = prefix + uuid();
            storage.setItem(nextKey, JSON.stringify({...content, updated_at: now()}));
            const previousKey = journalKey;
            journalKey = nextKey;
            journalContent = serialized;
            // 原 key 从创建后从不修改；并行页面的新意图一定在另一个 key 下。
            if (previousKey) storage.removeItem(previousKey);
          }
          // 新日志持久化后可收拢完全相同的来源，离线反复重开也不会无限增殖。
          if (!ambiguousRecovery) clearSources();
        } else {
          if (journalKey) storage.removeItem(journalKey);
          journalKey = journalContent = null;
        }
        return true;
      } catch { persistenceError = true; return false; }
    }
    function clearSources() {
      for (const entry of sources) {
        // 只回收已观察的不可变前驱；新快照具有新 key，不会被旧回执删除。
        try { if (entry.key !== journalKey) storage.removeItem(entry.key); }
        catch { persistenceError = true; }
      }
      sources = [];
    }
    function cacheRecord() {
      if (!record) return;
      try {
        const raw = readStorage(cacheKey);
        const cached = raw ? validateRecord(JSON.parse(raw), novelId) : null;
        if (!cached || cached.version < record.version) storage.setItem(cacheKey, JSON.stringify(record));
      } catch {
        try { storage.setItem(cacheKey, JSON.stringify(record)); }
        catch { persistenceError = true; }
      }
    }
    function acceptRecord(value) {
      const checked = validateRecord(value, novelId);
      if (!record || checked.version >= record.version) record = checked;
      cacheRecord();
      return checked;
    }
    function markConflict(reason = 'updated_elsewhere') {
      phase = 'conflict';
      conflict = {reason, record: copy(record)};
      lastError = reason === 'multiple_local' ? '本机有多个未同步位置，请确认要保留的进度。'
        : '其他设备或窗口已更新阅读进度，请选择要保留的位置。';
      persist();
    }
    function schedule(delay) {
      if (disposed || timer !== null || conflict || ['auth', 'unsupported'].includes(phase) || !(pending || dirty)) return;
      timer = later(() => { timer = null; void flush(); }, delay ?? Math.max(0, 5000 - (now() - lastSentAt)));
    }
    function stopTimer() { if (timer !== null) cancelTimer(timer); timer = null; }

    function edit(method, progress, force = false) {
      if (method === 'POST' && (!integer(progress) || progress > 100)) throw new Error('无效的阅读进度。');
      const status = progressStatus(progress);
      const target = localIntent();
      if (!force && target?.method === method && (method === 'DELETE' || (target.progress === progress && target.status === status))) return;
      if (!force && !target && record && viewVersion === record.version && method === 'POST'
        && record.progress === progress && record.status === status) return;
      const base = dirty ? dirty.base_version : (pending ? pending.base_version + 1 : viewVersion);
      dirty = {method, base_version: base, ...(pending ? {parent: pending.mutation_id} : {}),
        ...(method === 'POST' ? {progress, status} : {})};
      if (!conflict && !['auth', 'unsupported'].includes(phase)) phase = initialized ? 'pending' : 'checking';
      persist();
      notify();
      schedule();
    }

    async function refresh({initial = false} = {}) {
      if (reading) return reading;
      const first = !initialized;
      reading = (async () => {
        try {
          const response = await options.request(endpoint, {method: 'GET', cache: 'no-store'});
          const data = await response.json();
          if (!response.ok) {
            const error = new Error(options.errorText?.(data, response) || data?.detail || '读取阅读进度失败');
            error.status = response.status;
            throw error;
          }
          const checked = acceptRecord(data);
          initialized = true;
          lastError = '';
          if (ambiguousRecovery) markConflict('multiple_local');
          else if (dirty && !pending) {
            // 第一次 GET 之前的滚动不能自动套用服务器已有的新版本。
            if (dirty.base_version === null && checked.version === 0 && !dirty.legacy) dirty.base_version = 0;
            if (dirty.base_version !== record.version) markConflict();
            else { conflict = null; phase = 'pending'; }
          } else if (!pending) {
            if (!first && viewVersion !== null && viewVersion !== record.version) markConflict();
            else { viewVersion = record.version; conflict = null; phase = 'synced'; }
          } else if (!conflict) phase = 'pending';
          persist();
          notify();
          schedule();
          return {record: copy(record), restore: (first || initial) && !(pending || dirty || conflict), offline: false};
        } catch (error) {
          initialized = true;
          if (!conflict) phase = error.status === 401 || error.status === 403 ? 'auth'
            : error.status ? 'error' : (error.message?.includes('服务器尚不支持') ? 'unsupported' : 'offline');
          lastError = error.message || '网络不可用，进度保留在本机等待同步。';
          notify();
          return {record: copy(record), restore: (first || initial) && !(pending || dirty || conflict), offline: true};
        } finally { reading = null; }
      })();
      return reading;
    }

    async function writeOne({keepalive = false} = {}) {
      if (!pending && dirty) {
        if (!integer(dirty.base_version) || dirty.parent) return {queued: true};
        pending = {method: dirty.method, base_version: dirty.base_version, mutation_id: uuid(),
          ...(dirty.method === 'POST' ? {progress: dirty.progress, status: dirty.status} : {})};
        dirty = null;
      }
      if (!pending) return {ok: true, record: copy(record), unchanged: true};
      const sent = copy(pending);
      const body = {base_version: sent.base_version, mutation_id: sent.mutation_id,
        ...(sent.method === 'POST' ? {progress: sent.progress, status: sent.status} : {})};
      persist();
      lastSentAt = now();
      phase = 'syncing';
      busy = true;
      lastError = '';
      notify();
      try {
        const response = await options.request(endpoint, {method: sent.method, keepalive,
          headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        const data = await response.json();
        if (response.status === 409 && ['progress_conflict', 'progress_idempotency_conflict'].includes(data?.error)) {
          acceptRecord(data.record);
          markConflict(data.error);
          return {conflict: true, error: lastError};
        }
        if (!response.ok || data?.ok === false || data?.success === false) {
          const error = new Error(options.errorText?.(data, response) || data?.detail || '保存阅读进度失败');
          error.status = response.status;
          throw error;
        }
        if (data?.ok !== true || data?.success !== true || typeof data.replayed !== 'boolean'
          || data.applied_version !== sent.base_version + 1) {
          throw new Error('服务器尚不支持可靠的阅读进度同步，请刷新或稍后重试。');
        }
        const confirmed = validateRecord(data.record, novelId);
        if (confirmed.version < data.applied_version) throw new Error('服务器返回了无效的阅读进度版本。');
        acceptRecord(confirmed);
        pending = null;
        if (dirty?.parent === sent.mutation_id) delete dirty.parent;
        if (record.version > data.applied_version) {
          // 已成功的旧 mutation 可以确认；它的迟到回执不能替后来样本取得新版本。
          if (!dirty) dirty = {method: sent.method, base_version: data.applied_version,
            ...(sent.method === 'POST' ? {progress: sent.progress, status: sent.status} : {})};
          markConflict();
        } else {
          viewVersion = data.applied_version;
          conflict = null;
          phase = dirty ? 'pending' : 'synced';
        }
        if (persist()) clearSources();
        if (!disposed) options.onCommit?.({method: sent.method, record: copy(record),
          appliedVersion: data.applied_version, current: !conflict, hasPending: !!dirty});
        return conflict ? {conflict: true, error: lastError} : {ok: true, record: copy(record), queued: !!dirty};
      } catch (error) {
        phase = error.status === 401 || error.status === 403 ? 'auth'
          : error.message?.includes('服务器尚不支持') || error.message?.includes('无效的阅读进度版本') ? 'unsupported'
          : error.status ? 'error' : 'offline';
        lastError = error.message || '网络不可用，进度保留在本机等待同步。';
        persist();
        return {error: lastError, queued: true};
      } finally { busy = false; notify(); }
    }

    async function flush({explicit = false, keepalive = false} = {}) {
      if (writing) return writing;
      stopTimer();
      if (conflict) return {conflict: true, error: lastError};
      if (!initialized) {
        await refresh();
        if (writing) return writing;
        if (conflict) return {conflict: true, error: lastError};
      }
      if (phase === 'unsupported' || (phase === 'auth' && !explicit)) return {error: lastError, queued: true};
      writing = (async () => {
        let result = await writeOne({keepalive});
        // 显式保存/重置最多收口一份已在途修改和一份后续样本。
        if (explicit && result.ok && dirty && !conflict) result = await writeOne({keepalive});
        return result;
      })();
      try { return await writing; }
      finally {
        writing = null;
        if (phase === 'pending') schedule();
      }
    }

    function useServer() {
      if (!record || busy) return null;
      stopTimer();
      pending = dirty = conflict = null;
      ambiguousRecovery = false;
      viewVersion = record.version;
      phase = 'synced';
      lastError = '';
      if (persist()) clearSources();
      notify();
      return copy(record);
    }
    async function keepLocal(fallbackProgress) {
      if (!record || busy) return {queued: true};
      const desired = copy(localIntent()) || {method: 'POST', progress: fallbackProgress, status: progressStatus(fallbackProgress)};
      if (!validIntent({...desired, base_version: record.version})) return {error: '尚无可同步的本机进度。'};
      pending = null;
      dirty = {method: desired.method, base_version: record.version,
        ...(desired.method === 'POST' ? {progress: desired.progress, status: desired.status} : {})};
      viewVersion = record.version;
      conflict = null;
      ambiguousRecovery = false;
      phase = 'pending';
      if (persist()) clearSources();
      notify();
      return flush({explicit: true});
    }
    notify();
    return {
      state, refresh, flush, useServer, keepLocal,
      sample: progress => edit('POST', progress),
      async save(progress) { edit('POST', progress); return flush({explicit: true}); },
      async reset() { edit('DELETE', 0); return flush({explicit: true}); },
      storageChanged(event) {
        // 他页的本地日志不代表服务器已变化，避免 storage→GET→日志写入反馈。
        if (event?.key === cacheKey) return refresh();
        return Promise.resolve();
      },
      dispose() { disposed = true; stopTimer(); },
    };
  }

  return {create, validateRecord, progressStatus};
});
