# Release Blocker Fixes Design

**Status:** Approved in conversation on 2026-07-14

**Goal:** Make the two local commits safe to push by fixing confirmed security,
transaction, UI, test-isolation, and documentation regressions without broad
architectural restructuring.

## Scope

This change fixes only release blockers found by the repository audit:

1. Reject unauthenticated requests carrying proxy headers unless proxy trust is
   explicitly enabled; no untrusted X-Forwarded-For local-address bypass remains.
2. Make AI state parsing and wizard import validated, bounded, and atomic.
3. Bind AI Provider connections to the IP addresses that passed SSRF validation,
   preserve the original Host/SNI for TLS, reject every non-global destination by
   default, and never follow redirects carrying model requests or credentials.
4. Preserve mode `0600` for every atomic `.env` writer through one shared helper.
5. Restore wizard/distill navigation without reverting the approved AI project UI.
6. Make the default pytest command independent of repository-local databases,
   storage directories, and live DNS.
7. Correct high-impact README/API/deployment claims, mark historical documents as
   snapshots, add the declared MIT license, and use package version `0.1.0` as the
   runtime version source.

The change does not split the large Vue template, regenerate a 173-endpoint API
manual, rebuild the knowledge graph, or redesign the deployment architecture.

## Architecture

### Trusted Proxy Boundary

When `DASHBOARD_TRUST_PROXY` is false, the presence of `X-Forwarded-For` or
`X-Real-IP` makes an unauthenticated request non-local and therefore forbidden.
When trust is true, `_client_addr()` remains the single source of the client IP and
uses the configured right-to-left hop count. Tests cover both branches.

### Provider Transport

URL parsing and DNS resolution produce a validated target containing the original
hostname and one already-checked IP address. A Requests HTTP adapter connects the
urllib3 pool to that IP while retaining the original hostname for the HTTP `Host`
header, TLS SNI, and certificate verification. This removes the second DNS lookup
that enabled rebinding. Every request attempt resolves and pins again, all 3xx
responses are rejected, and automatic redirects are disabled.

By default an address is allowed only when `ipaddress.is_global` is true. The
existing private-host opt-in permits only private and loopback destinations;
link-local, multicast, unspecified, reserved, and shared non-global ranges remain
blocked.

### Atomic AI Persistence

State parsing keeps one foreshadow counter across all repeated sections and wraps
state plus foreshadow writes in `Database.transaction()`. Wizard import validates
the complete project/chapter/foreshadow payload before creating anything, clips
before deduplication, then performs project creation, child writes, and session
status update in one transaction. Invalid input leaves no partial records.

### Secure Environment Writes

A small utility performs atomic byte writes using a freshly created temporary file,
mode `0600`, full-write semantics, `fsync`, `os.replace`, and final chmod. OAuth,
Flask secret initialization, and Web Cookie persistence all use it. The helper does
not follow a pre-created temporary symlink.

### Test Isolation

An autouse pytest fixture points database and storage environment variables at each
test's temporary directory. Provider fallback tests mock deterministic public DNS.
Regression tests are added before production fixes and must be observed failing for
the intended reason.

## Documentation And Repository Hygiene

README examples will either match current authenticated APIs or direct users to the
Dashboard/authoritative contract. `API_COMPLETE.md`, `KNOWLEDGE_GRAPH.md`, and the
old AI implementation plan receive explicit historical-snapshot banners and the
index stops presenting them as current truth. Deployment documentation identifies
the root deployment script as canonical and corrects its public port. Shell/config
files receive LF attributes and executable scripts receive executable Git modes.

Local `.claude/`, `memory/`, and backup configuration files are ignored. Existing
untracked user files and runtime databases are neither deleted nor committed.

## Verification

Each fix follows red-green TDD with focused tests. Final verification runs in the
isolated worktree with temporary storage and includes Python AST/compile checks,
the complete pytest suite, Git whitespace/object checks, CLI help, Bash syntax, and
a clean Git status review. The remote is fetched immediately before deciding to
push; push is allowed only if all blocker tests and the full suite pass.
