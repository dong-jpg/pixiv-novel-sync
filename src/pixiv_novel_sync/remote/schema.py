"""Independent control database schema; never applied to the library database."""

SCHEMA_VERSION = 2

# Each statement is executed inside the caller's migration transaction. executescript
# would implicitly commit and is intentionally avoided here.
STATEMENTS = [
    """CREATE TABLE meta (
        id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL,
        next_fence INTEGER NOT NULL CHECK(next_fence>0))""",
    """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, boot_id TEXT NOT NULL,
        token_hash TEXT NOT NULL CHECK(length(token_hash)=64), epoch TEXT NOT NULL,
        protocol INTEGER NOT NULL, renderer TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active','retired','expired')),
        created_at REAL NOT NULL, expires_at REAL NOT NULL, last_seen_at REAL NOT NULL)""",
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY, scope TEXT NOT NULL DEFAULT 'dashboard' CHECK(scope='dashboard'),
        idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
        kind TEXT NOT NULL CHECK(kind IN ('diagnostic.v1','epub.single.v1')),
        input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
        state TEXT NOT NULL CHECK(state IN ('queued','active','succeeded','failed','cancelled','expired')),
        created_at REAL NOT NULL, not_before REAL NOT NULL, deadline REAL NOT NULL,
        finished_at REAL, error_code TEXT)""",
    """CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
        attempt_no INTEGER NOT NULL CHECK(attempt_no BETWEEN 1 AND 3),
        machine_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(session_id),
        claim_key TEXT NOT NULL, epoch TEXT NOT NULL, fence INTEGER NOT NULL CHECK(fence>0),
        state TEXT NOT NULL CHECK(state IN ('active','succeeded','failed','lost','cancelled','expired')),
        claimed_at REAL NOT NULL, lease_until REAL NOT NULL, hard_deadline REAL NOT NULL,
        heartbeat_seq INTEGER NOT NULL DEFAULT 0, heartbeat_receipt_json TEXT,
        ended_at REAL, error_code TEXT)""",
    """CREATE TABLE artifacts (
        attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id), chunk_id INTEGER NOT NULL CHECK(chunk_id=0),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64), size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 33554432),
        blob_ref TEXT NOT NULL CHECK(length(blob_ref)=64),
        state TEXT NOT NULL CHECK(state IN ('staged','published','expired')),
        received_at REAL NOT NULL, published_at REAL, PRIMARY KEY(attempt_id,chunk_id))""",
    """CREATE TABLE machine_control (
        id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0,
        machine_id TEXT NOT NULL, activation_id TEXT, owned_run_started_at REAL,
        mode TEXT NOT NULL DEFAULT 'idle' CHECK(mode IN ('disabled','idle','starting','ready','draining','suspending','cooldown','error')),
        gate_ready_until REAL, runtime_budget_until REAL, hold_until REAL,
        idle_since REAL, next_poll_at REAL, last_provider_at REAL, provider_state TEXT,
        cooldown_until REAL, last_error TEXT, action_json TEXT, leader_id TEXT, leader_until REAL,
        runtime_ready INTEGER NOT NULL DEFAULT 0 CHECK(runtime_ready IN (0,1)),
        provider_run_started_at REAL, activation_previous_run_started_at REAL, provider_operation_id TEXT,
        last_write_at REAL, last_wake_at REAL, backoff_count INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE blob_objects (
        sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64),
        size_bytes INTEGER NOT NULL CHECK(size_bytes>=0), created_at REAL NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('input','result')),
        state TEXT NOT NULL CHECK(state IN ('ready','deleting')))""",
    """CREATE TABLE input_parts (
        input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
        part TEXT NOT NULL CHECK(part IN ('manifest','text','cover')),
        blob_sha256 TEXT NOT NULL REFERENCES blob_objects(sha256),
        size_bytes INTEGER NOT NULL CHECK(size_bytes>=0), PRIMARY KEY(input_sha256,part))""",
    """CREATE TABLE blob_reservations (
        reservation_id TEXT PRIMARY KEY, purpose TEXT NOT NULL CHECK(purpose IN ('input','result')),
        owner_key TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
        expires_at REAL NOT NULL, UNIQUE(purpose,owner_key))""",
    "CREATE UNIQUE INDEX run_event ON runs(scope,idempotency_key)",
    "CREATE UNIQUE INDEX boot_identity ON sessions(machine_id,boot_id)",
    "CREATE UNIQUE INDEX session_secret ON sessions(token_hash)",
    "CREATE UNIQUE INDEX one_session ON sessions(machine_id) WHERE state='active'",
    "CREATE UNIQUE INDEX claim_receipt ON attempts(session_id,claim_key)",
    "CREATE UNIQUE INDEX attempt_order ON attempts(run_id,attempt_no)",
    "CREATE UNIQUE INDEX fence_identity ON attempts(epoch,fence)",
    "CREATE UNIQUE INDEX one_machine_attempt ON attempts(machine_id) WHERE state='active'",
    "CREATE UNIQUE INDEX one_run_attempt ON attempts(run_id) WHERE state='active'",
    "CREATE UNIQUE INDEX one_run_success ON attempts(run_id) WHERE state='succeeded'",
    "CREATE INDEX eligible_runs ON runs(state,not_before,created_at)",
    "CREATE INDEX input_blob_reference ON input_parts(blob_sha256)",
]
