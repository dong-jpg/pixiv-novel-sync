(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./ai_settings.js'));
  else root.AIProviderOnboarding = factory(root.AISettings);
})(typeof window === 'object' ? window : globalThis, function (AISettings) {
  'use strict';
  const clone = value => JSON.parse(JSON.stringify(value));
  function createOnboarding({request, waitForSync, readCatalog, onChange = () => {}}) {
    let state = {phase: 'idle', pending: false, providerId: null, operationId: null,
      selectedKeys: [], revision: null, missing: [], error: ''};
    let generation = 0, disposed = false, pendingPromise = null, reviewedPlan = null;
    const isCurrent = activeGeneration => !disposed && generation === activeGeneration;
    const publish = patch => {
      if (disposed) return;
      state = clone({...state, ...patch});
      onChange(clone(state));
    };
    const run = task => {
      if (pendingPromise) return pendingPromise;
      if (disposed) return Promise.resolve(clone(state));
      const activeGeneration = generation;
      pendingPromise = Promise.resolve().then(() => {
        if (isCurrent(activeGeneration)) return task(activeGeneration);
      }).finally(() => { pendingPromise = null; });
      return pendingPromise;
    };
    const applyFailure = error => publish({phase: error.status === 409 ? 'conflict' : 'apply_failed',
      pending: false, error: error.message || 'Model selection was not confirmed; refresh and review before retrying.'});
    async function applySelection(plan, activeGeneration) {
      if (!isCurrent(activeGeneration)) return;
      if (!plan.changes.length) {
        publish({phase: 'selection_unavailable', missing: plan.missing, pending: false, error: ''});
        return;
      }
      publish({phase: 'applying', revision: plan.revision, missing: plan.missing, error: ''});
      await request(`/api/dashboard/ai/providers/${state.providerId}/models/enabled`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({catalog_revision: plan.revision, changes: plan.changes}),
      });
      if (!isCurrent(activeGeneration)) return;
      publish({phase: 'applied', pending: false, error: ''});
    }
    async function loadSelection(activeGeneration, review) {
      if (!isCurrent(activeGeneration)) return;
      publish({pending: true, error: ''});
      try {
        const catalogue = await readCatalog(state.providerId);
        if (!isCurrent(activeGeneration)) return;
        const plan = AISettings.initialModelSelection(catalogue, state.selectedKeys);
        if (review) {
          reviewedPlan = plan;
          publish({phase: 'confirm_revision', pending: false, revision: plan.revision, missing: plan.missing});
        } else await applySelection(plan, activeGeneration);
      } catch (error) { if (isCurrent(activeGeneration)) applyFailure(error); }
    }
    async function syncSelection(activeGeneration) {
      if (!isCurrent(activeGeneration)) return;
      publish({phase: 'syncing', pending: true, error: ''});
      let terminalFailure = false;
      try {
        if (!state.operationId) {
          const operation = await request(`/api/dashboard/ai/providers/${state.providerId}/models/sync`, {method: 'POST'});
          if (!isCurrent(activeGeneration)) return;
          if (!operation || !operation.operation_id) throw new Error('Sync response is missing its operation ID.');
          publish({operationId: operation.operation_id});
        }
        if (!isCurrent(activeGeneration)) return;
        const operation = await waitForSync(state.providerId, state.operationId);
        if (!isCurrent(activeGeneration)) return;
        if (operation && operation.status === 'needs_empty_confirmation') {
          publish({phase: 'awaiting_empty_confirmation', pending: false});
          return;
        }
        if (!operation || operation.status !== 'succeeded') {
          terminalFailure = operation && ['failed', 'cancelled'].includes(operation.status);
          throw new Error(operation?.error_message || 'Sync did not complete successfully; the saved provider is retained.');
        }
      } catch (error) {
        if (isCurrent(activeGeneration)) publish({phase: 'sync_failed', pending: false,
          operationId: terminalFailure ? null : state.operationId, error: error.message});
        return;
      }
      if (!isCurrent(activeGeneration)) return;
      await loadSelection(activeGeneration, false);
    }
    function save(form, selectedKeys) {
      if (pendingPromise) return pendingPromise;
      if (disposed || !['idle', 'save_failed'].includes(state.phase)) return Promise.resolve(clone(state));
      const existingId = Number.isSafeInteger(form.id) && form.id > 0 ? form.id : null;
      const payload = {...form};
      delete payload.id;
      const body = JSON.stringify(payload);
      return run(async activeGeneration => {
        publish({phase: 'saving', pending: true, providerId: existingId,
          selectedKeys: [...new Set(selectedKeys)], missing: [], error: ''});
        try {
          const saved = await request('/api/dashboard/ai/providers' + (existingId ? `/${existingId}` : ''), {
            method: existingId ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body,
          });
          if (!isCurrent(activeGeneration)) return;
          const providerId = existingId || saved?.id;
          if (!Number.isSafeInteger(providerId) || providerId < 1) throw new Error('Provider creation was not confirmed.');
          publish({phase: 'saved', providerId});
        } catch (error) {
          if (!isCurrent(activeGeneration)) return;
          const uncertain = !existingId && (!error.status || error.status >= 500);
          publish({phase: uncertain ? 'create_unconfirmed' : 'save_failed', pending: false,
            error: uncertain ? 'Creation was not confirmed. Refresh providers and check for an existing instance before creating again.'
              : error.message || 'Provider save failed.'});
          return;
        }
        if (!isCurrent(activeGeneration)) return;
        if (!state.selectedKeys.length) { publish({phase: 'saved_only', pending: false}); return; }
        await syncSelection(activeGeneration);
      });
    }
    function retry() {
      return run(async activeGeneration => {
        if (['sync_failed', 'awaiting_empty_confirmation'].includes(state.phase)) await syncSelection(activeGeneration);
        else if (['apply_failed', 'conflict', 'selection_unavailable'].includes(state.phase)) await loadSelection(activeGeneration, true);
      });
    }
    function confirmRevision() {
      return run(async activeGeneration => {
        if (state.phase !== 'confirm_revision' || !reviewedPlan) return;
        const plan = reviewedPlan;
        reviewedPlan = null;
        publish({pending: true});
        try { await applySelection(plan, activeGeneration); }
        catch (error) { if (isCurrent(activeGeneration)) applyFailure(error); }
      });
    }
    return {save, retry, confirmRevision, getState: () => clone(state), dispose() { disposed = true; generation++; reviewedPlan = null; }};
  }
  return {createOnboarding};
});
