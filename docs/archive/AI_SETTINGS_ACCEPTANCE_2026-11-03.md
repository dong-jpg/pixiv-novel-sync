# AI Settings And Invocation Acceptance (2026-11-03)

This records phase-1 application acceptance, not a deployment claim. Current
release and production facts are maintained in [STATUS](../STATUS.md).

## Scope And Identity

- Approved specification: [phase-1 design](../superpowers/specs/2026-10-31-ai-settings-and-invocation-design.md).
- Executed work: all eight tasks in the [implementation plan](../superpowers/plans/2026-11-01-ai-settings-and-invocation.md), with individual reviews and final independent whole-feature review.
- Verified application commit: `3c51a6c6107a61b95cdbf58a00044506f405bccc`.
- Review/deployment comparison base: `16b5b4a13fb34e53299a45a09469a5bff6dfff87`.
- Work branch: `codex/ai-settings-resume`; merge `2167c585` preserves the audited main history. No reset, rebase-away, force-push or primary-checkout rewrite is used.
- Phase 1 delivers independent AI configuration, shared model selection, per-candidate capacity planning, normalized text invocation, bounded preflight, reported usage and safe diagnostics. Keyword cleaning is the only integrated business consumer.
- Whole-book/series reading, cross-chapter evidence, XP-analysis agents and the recommendation loop remain phase 2. Main continuation remains an unsupported 409, without creating a job or a provider request.

## Independent Review

The whole-feature code/spec review is Approved at the verified application
commit, not merely at the last small patch. The final coherent repair and
follow-up guard close these findings:

- Actual HTTP timeout/deadline propagation through both protocols, normal and streaming reads, retry/fallback sends and cancellation boundaries.
- Current visible onboarding choices on explicit retry/confirmation; invalidated or unsaved-context previews cannot become accidental deselect-all.
- Exact opaque model keys, including trailing spaces, through picker and manual binding.
- Catalog search by effective displayed name while preserving full-catalog revision and identity.
- Agent editor ownership through confirmed writes and both readbacks; failed readback does not make a confirmed create falsely retryable.

The reviewer independently ran 35 final actual-page Node cases. Earlier
integration cancellation/CAS/privacy and scoped UI findings are closed. No
remaining actionable code finding is recorded. Release operations retain a
separate safety gate.

## Current-Commit Verification

| Check | Result | Environment and boundary |
| --- | --- | --- |
| Full Python suite | 2414 passed, 52 skipped; exit 0; 351.21s | Windows, Python 3.11.3 |
| Full Python suite | 2363 passed, 103 skipped; exit 0; 218.80s | Native WSL Linux snapshot from the exact Git archive, isolated Python 3.10.12 |
| Eight standalone Node suites | 429 passed, 0 failed/skipped/cancelled; exit 0; 12.55s | Node 22.22.1; the legacy reader file is a file-level case in this total |
| Legacy reader direct mode | Exit 0 | Run separately using its ordinary wrapper entry point |
| Compileall | Exit 0 | Byte-identical isolated copy of all 77 packaged Python sources |
| Pyflakes | Exit 0, no diagnostics | Actual current `src`, pyflakes 3.4.0 |
| Tailwind | Check and independent raw-byte regeneration match | Cached pinned CLI 3.4.17; no tracked generated rewrite |
| Offline wheel | Build exit 0; all 107 packaged source/assets match | Bundled Python 3.12.14, no index/dependencies/build isolation |

Skip accounting is explicit, not a success count:

- Both platforms skip 10 non-streaming parameter combinations that only apply to streaming and 36 explicitly opt-in AI browser cases with `AI_UI_VUE_JS` unset.
- Windows additionally skips six symlink/POSIX-mode cases; these are covered by the successful Linux suite.
- Linux additionally skips 56 existing Chromium cases because its browser binary is absent, and one `Connection.setlimit` case requiring Python 3.11. Windows covers those existing browser/API combinations separately.
- A prior isolated Flask 3.0.3 / Werkzeug 3.1.8 API subset passed 59 cases. This is selected minimum-Flask API evidence, not a full-project minimum-version certification.

The artifact harness first encountered Windows path-length errors in bytecode
and pip temporary paths. Failed logs remain preserved. Byte-identical isolated
source compilation and a short child-only wheel TEMP/TMP directory resolved
the harness limitations; no product source, global environment or OS policy
was changed to hide a failure.

## Browser Evidence

Actual shipped Jinja/Vue and local SRI-verified Vue 3.4.38 run in existing
Chromium against strictly synthetic API/text fixtures at 375, 768 and 1440px.
No configured application, real provider, Pixiv, private novel or user database
is opened by these browser tests.

- Frozen baseline: 33 passed in 473.70s; all 17 before/after asset hashes match.
- Subsequent malformed-plan and candidate-chain fixes: each passes its relevant three-width cohort with stable assets.
- Final onboarding selection, exact-key picker and save/readback ownership fixes: their relevant three-width cohorts pass; the locking case increases the final file to 36 cases.
- Latest invalid-preview-intent follow-up: 3 passed in 9.19s, all relevant assets stable; deliberate valid deselect-all still works.
- Final browser file SHA256: `06b01429fa2b2bdc21c59517ede0f174657ff3d6801308a68155cfedeb1f7eb1`.

This is a frozen baseline plus focused revalidation and asset reconciliation,
not one complete final-head 36-case execution. Node VM tests are not browser
rendering, and desktop touch emulation is not a real-device soft-keyboard test.

## Artifact Identity

- CSS: 39383 bytes, SHA256 `b402e990f1a2168bd970de327df4966900880696250efeeb8be4e3626fdafe40`; template cache version `b402e990f1a2`.
- Fresh wheel: `pixiv_novel_sync-0.1.0-py3-none-any.whl`, 560883 bytes, SHA256 `34d40d693984947c24f48766dbbad48354dfd83c31281f15ca7e7bcebc5840a7`.
- Packaged source: 77 Python, 24 HTML, 5 JavaScript and 1 CSS files; all 107 match the verified checkout bytes. No bytecode is packaged.
- All 113 wheel member payloads also match the preceding candidate wheel. Only six dist-info ZIP timestamps differ; an archive-hash difference is not misreported as a source difference.
- All 367 tracked-file fingerprints remained unchanged during artifact validation.

Detailed command logs, hashes, manifests and review reports remain in the
managed worktree's ignored `.superpowers/sdd/` and `.superpowers/verification/`
directories. No secrets or production database copies enter Git.

## Release And External Boundaries

At this acceptance record, origin/main and production remain at the comparison
base. Read-only preflight confirms clean tracked production source, an active
service with no restart, no active AI/model-sync/task rows, no extra project
worker/timer, correct editable source and dependencies, config mode 0600, and
the original Nginx configuration passing `nginx -t`.

Release must still use private on-server migration/restore rehearsal, a fresh
stopped-service SQLite backup, ordinary fast-forward source update, an
all-request ingress fence, no-scheduler validation and explicit authenticated
health/assets checks. No-scheduler validation is not read-only: startup
reconciliation and authentication legitimately mutate the post-backup DB.
Temporary validation suppresses only the startup trash sweep. Automatic old-DB
restore is forbidden once the normal scheduler starts, even while ingress is
still fenced.

Real provider 1M admission, actual billed usage, real-network cancellation,
real-device soft keyboards and broader historical project backlog remain
external or separately scoped. No paid/private-content acceptance is implied.
The primary checkout's six dirty documents, extra application document and
`.probe012/` are preserved; implementation is isolated in the managed worktree.

## Canonical Release Verification Addendum (2026-11-04)

The preceding sections preserve the original acceptance checkpoint and its
then-current release boundary. This addendum records later canonical-byte
verification; it does not retroactively replace those valid test results or
claim that deployment, scheduler resumption or public release is complete.
Consult [STATUS](../STATUS.md) for actual release facts.

### Archive And Wheel Identity

Strict server-side staging checks caught Windows Git end-of-line conversion
before any live release changes. In the original Windows-generated archive,
360 of 368 files differed from their Git blobs only by CRLF versus LF. This
was an artifact-byte identity issue, not a newly discovered application bug.
The replacement archive was generated with per-command `core.autocrlf=false`
and `core.eol=lf`; all 368 archived file payloads and Git modes match candidate
commit `793d340d6d1fe0028c092ff3c9b8c063bb8586e8`.

- Canonical archive: `release-793d340d-canonical.tar`.
- Canonical archive SHA256: `4d05501bacf2cbf3ae5c0160676992ee79565122da3097c070771cc717bd9d16`.
- The earlier wheel's 107 packaged source/asset comparisons remain valid against the tested Windows checkout. They do not establish raw-byte identity with this canonical LF release target. No canonical-target wheel identity is inferred from that earlier wheel result.

### Canonical Linux And Full Browser Results

| Check | Result | Boundary |
| --- | --- | --- |
| Canonical full Python suite | 2363 passed, 103 skipped; exit 0; 176.99s | Isolated native Linux run against canonical archive bytes; the same 103-skip classification documented above applies |
| Final complete AI browser suite | 36 passed, 0 skipped/failed/errors; exit 0; 97.13s | One complete run of all 12 tests at 375, 768 and 1440px, using unchanged final fixtures and canonical shipped assets in existing Chromium 148.0.7778.96 |
| Browser source and asset stability | All 368 extracted tracked files and all 17 relevant assets unchanged before/after | Every extracted tracked file matched its candidate Git blob ID; the source files were marked read-only |

The all-36 run is new complete-final-fixture evidence, superseding the earlier
coverage limitation without recharacterizing the historical frozen-33 and
focused-cohort results. It ran from a short isolated copy, not the configured
application or original checkout, with the existing local Vue 3.4.38 runtime
verified against shipped SRI. The unchanged synthetic request whitelist,
browser-error assertions and restricted operation-ID storage assertions all
passed. No user database, private novel, real Provider/Pixiv request, paid
generation or installed/downloaded browser was involved.

The managed worktree retains the new ignored report
`.superpowers/sdd/canonical-browser-release-report.md` and complete evidence
under `.superpowers/verification/canonical-browser-20261104/`: exact command
and environment, Chromium version, stdout/stderr, exit code, UTC timestamps,
and before/after 17-asset and 368-file manifests. The run started at
`2026-11-04T02:22:47.915889+00:00` and finished at
`2026-11-04T02:24:25.781484+00:00`; measured subprocess wall time was 97.859s.
The matching 17-asset manifest SHA256 is
`e7e8fa28deca9d3d3e1ed73da85534bc9053ed4ab526250b975d5cb7a7684960`.
The browser test file retains the SHA256 recorded above.

### Separate Public Cache Release Gate

An actual public Cloudflare check subsequently found that bare JavaScript
responses had correct content hashes but origin `no-cache` was rewritten to
`max-age=14400`. Correct bytes at one observation do not establish safe
future release caching. A narrow Nginx correction for the five packaged
JavaScript assets, using `private`/`no-store`, was approved and verified with
59 focused tests plus the root's 45-test verification. This is deployment
configuration and test evidence, with no JavaScript or Python application
source changes; operational proof of the corrected live public headers is
still pending at this addendum's checkpoint.

Neither the canonical test passes nor the cache-policy regression checks
claim a completed cutover or resumed scheduler. Real-device soft-keyboard
behavior and genuine paid 1M-context admission/usage remain manual external
acceptance. Release markers, migration/restore evidence, effective origin
and public headers, and scheduler state belong to the separate release
record and [STATUS](../STATUS.md), not an inferred success in this archive.
