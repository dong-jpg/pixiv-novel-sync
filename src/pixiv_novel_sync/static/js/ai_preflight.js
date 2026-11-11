(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AIPreflight = api;
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const allowedParameters = new Set(['temperature', 'top_p', 'max_tokens', 'allowed_provider_ids']);
  function buildPreflightBody(messages, parameters = {}) {
    if (!Array.isArray(messages) || messages.length < 1 || messages.length > 256) throw new RangeError('Provide 1 to 256 text messages');
    for (const message of messages) {
      if (!message || !['system', 'user', 'assistant'].includes(message.role) || typeof message.content !== 'string' ||
          Object.keys(message).some(key => !['role', 'content'].includes(key))) throw new TypeError('Only complete system/user/assistant text messages are supported');
    }
    if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)) throw new TypeError('Invalid preflight parameters');
    for (const [key, value] of Object.entries(parameters)) {
      if (!allowedParameters.has(key)) throw new TypeError('Unsupported preflight parameter');
      if (key === 'allowed_provider_ids') {
        if (!Array.isArray(value) || value.some(id => !Number.isSafeInteger(id) || id < 1)) throw new TypeError('Provider restrictions must be positive integer IDs');
      } else if (typeof value !== 'number' || !Number.isFinite(value) ||
          (key === 'max_tokens' && (!Number.isInteger(value) || value < 1))) throw new TypeError('Generation parameters must be finite numbers; max_tokens must be a positive integer');
    }
    const body = JSON.stringify({messages, ...parameters});
    if (new TextEncoder().encode(body).byteLength > 16 * 1024 * 1024) throw new RangeError('Preflight request exceeds 16 MiB');
    return body;
  }
  function createPreflight({request, onChange = () => {}}) {
    let generation = 0, disposed = false, abort = null;
    let state = {busy: false, result: null, error: ''};
    const getState = () => ({...state});
    const publish = patch => { state = {...state, ...patch}; if (!disposed) onChange(getState()); };
    async function run(agentId, messages, parameters = {}) {
      if (disposed) return;
      const active = ++generation;
      if (abort) abort.abort();
      const controller = abort = new AbortController();
      publish({busy: true, result: null, error: ''});
      let sending = false;
      try {
        if (!Number.isSafeInteger(agentId) || agentId < 1) throw new TypeError('Select a saved AI task');
        const body = buildPreflightBody(messages, parameters);
        sending = true;
        const result = await request(`/api/dashboard/ai/agents/${agentId}/preflight`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body, signal: controller.signal,
        });
        if (disposed || active !== generation) return;
        if (!result || result.version !== 2 || typeof result.can_run !== 'boolean' || !Array.isArray(result.eligible) || !Array.isArray(result.excluded)) throw new TypeError('Invalid preflight response');
        publish({result});
      } catch (error) {
        if (!disposed && active === generation && error?.name !== 'AbortError') {
          // Transport text may contain rejected input. Only local validation text is displayed verbatim.
          publish({error: !sending ? error.message : error.status === 413 ? 'Preflight request exceeds 16 MiB'
            : error.status === 400 ? 'Preflight input was rejected' : error.status === 404 ? 'Select a saved AI task' : 'Preflight failed'});
        }
      } finally {
        if (!disposed && active === generation) publish({busy: false});
      }
    }
    function reset() { ++generation; if (abort) abort.abort(); publish({busy: false, result: null, error: ''}); }
    function dispose() { disposed = true; reset(); }
    return {run, reset, dispose, getState};
  }
  return {buildPreflightBody, createPreflight};
});
