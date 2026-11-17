(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AIJobDiagnostics = api;
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  function projectJobDiagnostics(job) {
    const raw = job && job.usage_summary;
    const integer = value => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
    const usage = raw ? {
      known_input_tokens: integer(raw.known_input_tokens), known_output_tokens: integer(raw.known_output_tokens),
      known_total_tokens: integer(raw.known_total_tokens), reported_requests: integer(raw.reported_requests),
      unreported_requests: integer(raw.unreported_requests), complete: raw.complete === true,
    } : null;
    const usage_status = !usage ? 'unknown' : usage.reported_requests === 0 && usage.unreported_requests === 0 ? 'not_sent'
      : usage.complete ? 'reported' : 'incomplete';
    return {
      validation_status: typeof job?.validation_status === 'string' ? job.validation_status : 'unknown',
      can_continue: job?.can_continue === true,
      continue_unavailable_reason: typeof job?.continue_unavailable_reason === 'string' ? job.continue_unavailable_reason : '',
      snapshot_version: job?.snapshot_version === 1 || job?.snapshot_version === 2 ? job.snapshot_version : null,
      invocation_plan: job?.invocation_plan && typeof job.invocation_plan === 'object' && !Array.isArray(job.invocation_plan) ? job.invocation_plan : null,
      plan_unavailable_reason: typeof job?.plan_unavailable_reason === 'string' ? job.plan_unavailable_reason : '',
      usage_status, usage_summary: usage,
    };
  }
  return {projectJobDiagnostics};
});
