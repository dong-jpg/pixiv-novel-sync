# AI Settings Deployment (2026-11-04)

Production acceptance completed at **15:57:29 Asia/Shanghai** (07:57:29 UTC).
Current status is maintained in [STATUS](../STATUS.md). This record distinguishes
the deployed application, its reviewed Nginx amendment and subsequent records.

## Version And Scope

- Previous server checkout: `16b5b4a13fb34e53299a45a09469a5bff6dfff87`.
- Application verification baseline: `3c51a6c6107a61b95cdbf58a00044506f405bccc`.
- Release candidate: `793d340d6d1fe0028c092ff3c9b8c063bb8586e8`; only five acceptance/status documents differ from the application baseline.
- Ordinary non-force main push and server fast-forward preserve both histories. No reset, clean, generic update/deploy script or dependency reinstall is used.
- Phase 1 delivers independent AI configuration, model/group selection, per-candidate preflight, unified text invocation, reported usage and safe diagnostics. Keyword cleaning remains the only integrated business consumer.
- Whole-book/series reading, cross-chapter evidence, XP-analysis agents and the recommendation loop remain phase 2. Main continuation remains unsupported without creating a job or provider request.
- The primary checkout's six dirty documents, extra application document and `.probe012/` remain untouched. The managed implementation worktree is retained.

## Verification And Export Correction

| Check | Result and boundary |
| --- | --- |
| Windows application full | 2414 passed / 52 explicitly classified skips, 351.21s, exit 0 |
| Canonical Linux target full | 2363 passed / 103 explicitly classified skips, 176.99s, exit 0 |
| Complete canonical Chromium suite | 36 passed / zero skips, 97.13s; all three widths, stable 17 assets and 368 source files |
| Eight Node suites | 429 passed / zero skips; legacy reader direct mode passes separately |
| Static and packaging | Compileall, actual-source pyflakes, raw-byte CSS regeneration and offline wheel pass |
| Nginx cache repair | 59 focused deployment/static/preflight checks; root independently runs 45 covering checks |
| Operational safeguards | 125 synthetic helper tests plus 15 subtests; independent amendment review runs 15 boundary tests |

Detailed skip accounting and historical versus final browser evidence are in
the [phase acceptance record](AI_SETTINGS_ACCEPTANCE_2026-11-03.md). Missing
optional browser/runtime combinations are not counted as successes.

Strict on-server provenance checks rejected the first Windows-EOL archive
before any service/config/database cutover. Every one of its 360 mismatches
was solely CRLF-to-LF conversion caused by Windows Git archive settings. The
rejected archive and extracted files remain private evidence. A per-command
`core.autocrlf=false` / `core.eol=lf` export, without global setting changes,
matches all 368 Git blobs and executable modes exactly. Its SHA256 is
`4d05501bacf2cbf3ae5c0160676992ee79565122da3097c070771cc717bd9d16`.

The canonical archive received its own Linux full and complete Chromium run.
The earlier wheel's 107 package comparisons are valid Windows-checkout payload
evidence, not a claim that those member bytes equal canonical LF source. The
deployment uses verified editable canonical source, not that wheel.

## Private Rehearsal And Retained Backup

- Stage: `/home/ubuntu/.local/state/pixiv-ai-releases/793d340d6d1f`, owned by the service user, mode 0700. Secrets/database copies remain on the server and are not committed or downloaded.
- The effective database is reconciled against the running process's initial environment and real application settings, not inferred solely from YAML.
- Consistent SQLite backup API copies a 2,840,309,760-byte database using a read-only source connection. Original, migrated and restored private copies are retained.
- The candidate initializes schema twice on its copy. Full source/candidate integrity checks, global foreign keys, enumerated authoritative table counts, required AI columns and byte-identical snapshot restoration all pass.
- Before candidate startup, all-request ingress fencing, process/timer/cron/job checks and stopped-service/no-open-DB-holder checks precede a fresh final SQLite backup.
- Fresh backup acceptance at 15:00:26 includes quick-check, global FK, counts, snapshot/config hashes and fsynced files/directories/marker. It is not the earlier rehearsal snapshot reused as a cutover backup.
- Retained material includes old source archive/bundle, original Nginx/systemd unit, dependency manifest, environment, YAML and explicit source/config/helper amendment evidence.

Live migration completes at 15:05:27. It preserves enumerated authoritative
counts, passes global FK, checks integrity of `ai_provider_models`, `ai_jobs`
and `ai_job_model_attempts` with their indexes, and matches the rehearsed schema.
The retained fresh backup is unchanged. These scoped live checks are not
described as an additional live full-database scan or new FTS/catalog rebuild.

## Maintenance And Cache Finding

The all-request maintenance fence finishes reloading at **14:49:41**; the
baseline service stops at **14:49:43**. Final public acceptance completes at
**15:57:29**, approximately **68 minutes** later. This is a real maintenance
window, not a zero-downtime or two-minute deployment claim. It includes fresh
backup verification and resolution of the public cache-policy failure.

The scheduler-disabled validation instance starts successfully after migration.
Its unchanged probe rejects static assets because Cloudflare turns the origin's
bare-JS `no-cache` into `max-age=14400`. All five JS files and CSS have correct
canonical bytes both locally and publicly; the failure is header policy, not
missing assets or failed schema migration. The fence remains active throughout.

The reviewed correction adds a single anchored Nginx location matching exactly
five unversioned AI JavaScript names. It hides the upstream Cache-Control and
sets `private, no-store, no-cache, max-age=0, must-revalidate`, explicitly retains
HSTS and trusted proxy headers, and leaves CSS, request-body limits and all
other locations unchanged. A diagnostic URL first confirms Cloudflare respects
the policy; that diagnostic is removed and is not the final acceptance proof.

An explicit immutable amendment chain retains original prepared metadata and
normal/fenced configurations. It binds their hashes, reviewed template, unchanged
candidate/archive/backup/schema, and validation PID. Only the two effective
Nginx hashes plus the amendment reference may differ. Configurations are staged
and fsynced; effective prepared metadata activates last. The controller validates
this chain rather than silently replacing baseline evidence.

The actual five stable unversioned public URLs are then checked **twice** without
cache-busting query strings: all ten return 200, exact canonical bytes, the
strong policy and Cloudflare BYPASS. Keyless health stays 503 during the proof.
No probe assertion is relaxed and no backup or DDL is replayed.

## Normal Service Restoration

The remaining unchanged authenticated probe and denied-route checks pass at
15:54:58, producing the explicit scheduler-stopped validation marker. Temporary
validation suppresses only the startup trash sweep. Startup reconciliation and
authentication legitimately mutate the post-backup DB; this is not called
read-only validation.

The irreversible normal-start marker is written at **15:57:22**, before normal
CLI startup. The temporary override is removed, ordinary scheduling starts
while ingress remains fenced, and the authenticated running-state probe passes.
Only then is the fence removed and the keyless public probe rerun. No old DB
restore occurs after normal scheduling starts. Normal business work is observed
again in the independent final check.

| Production check | Observed result |
| --- | --- |
| Normal service | Active/running, PID 1401238, automatic restart count 0, no drop-ins |
| Local and public health | 200, status ok |
| Anonymous data endpoint | 401 |
| HTTPS login/settings/data | Accepted; both original settings URLs and database status 200 |
| Cookie and CSP | Secure, HttpOnly, SameSite=Lax; matching page CSP nonce |
| Assets | Five JS files and CSS match deployed bytes; JS revalidation/private policy passes publicly |
| Synthetic >1 MiB preflight | Guaranteed-missing Agent returns 404; proxy/body routing only, not model capacity |
| Probe session | Revoked after verification |
| Configuration | Environment/YAML bytes unchanged, mode 0600; original systemd unit bytes unchanged |
| Nginx | `nginx -t` passes; no fence/diagnostic remains; normal file restored to root:root / 0644 |

The Nginx policy, tests and these records are synchronized to Git after their
live verification. That follow-up changes no Python/JS/CSS application source
and requires no application restart. The application baseline remains fixed;
the final main commit also records the tested configuration amendment.

## Remaining Scope

Real-provider 1M admission, billing and network cancellation, real-device soft
keyboards, Cloudflare account-side TLS settings and the historical project
backlog remain separate acceptance work. The release probes do not invoke paid
generation, Pixiv tests or private-novel AI processing. Normal user-configured
business scheduling resumes after the declared boundary.
