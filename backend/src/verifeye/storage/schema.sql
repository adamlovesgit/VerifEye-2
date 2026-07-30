PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO schema_version(version) VALUES (1);
INSERT OR IGNORE INTO schema_version(version) VALUES (2);
INSERT OR IGNORE INTO schema_version(version) VALUES (3);
INSERT OR IGNORE INTO schema_version(version) VALUES (4);
INSERT OR IGNORE INTO schema_version(version) VALUES (5);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash BLOB NOT NULL,
    password_salt BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash BLOB PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_sessions_user_id ON sessions(user_id);

CREATE TABLE IF NOT EXISTS identities (
    id INTEGER PRIMARY KEY,
    external_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS face_embeddings (
    id INTEGER PRIMARY KEY,
    identity_id INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    dtype TEXT NOT NULL CHECK (dtype = 'float32'),
    vector BLOB NOT NULL,
    l2_norm REAL NOT NULL,
    source_path TEXT,
    detection_score REAL CHECK (
        detection_score IS NULL OR detection_score BETWEEN 0.0 AND 1.0
    ),
    metadata_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(metadata_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (length(vector) = dimensions * 4)
);

CREATE INDEX IF NOT EXISTS ix_face_embeddings_identity
    ON face_embeddings(identity_id);
CREATE INDEX IF NOT EXISTS ix_face_embeddings_model_dimensions
    ON face_embeddings(model_name, dimensions);

CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    encrypted_url BLOB NOT NULL UNIQUE,
    url_fingerprint TEXT NOT NULL UNIQUE,
    recognition_encrypted_url BLOB,
    recognition_url_fingerprint TEXT,
    sanitized_host TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'manual'
        CHECK (source_type IN ('manual', 'onvif')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS camera_events (
    id INTEGER PRIMARY KEY,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    source_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    state TEXT NOT NULL DEFAULT 'accepted'
        CHECK (state IN ('accepted', 'dispatched', 'completed', 'failed')),
    error_code TEXT,
    error_message TEXT,
    retention_exempt INTEGER NOT NULL DEFAULT 0 CHECK (retention_exempt IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(camera_id, source_event_id)
);

CREATE INDEX IF NOT EXISTS ix_camera_events_state_accepted
    ON camera_events(state, accepted_at);
CREATE INDEX IF NOT EXISTS ix_camera_events_camera_accepted
    ON camera_events(camera_id, accepted_at);

CREATE TABLE IF NOT EXISTS recognition_sessions (
    id INTEGER PRIMARY KEY,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'active', 'completed', 'failed', 'interrupted', 'cancelled')),
    interval_start TEXT NOT NULL,
    interval_end TEXT NOT NULL,
    maximum_end TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    stream_mode TEXT,
    runtime_generation INTEGER,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_recognition_sessions_camera_state
    ON recognition_sessions(camera_id, state);

CREATE TABLE IF NOT EXISTS recognition_session_events (
    session_id INTEGER NOT NULL REFERENCES recognition_sessions(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL UNIQUE REFERENCES camera_events(id) ON DELETE CASCADE,
    attribution_start TEXT NOT NULL,
    attribution_end TEXT NOT NULL,
    attached_at TEXT NOT NULL,
    PRIMARY KEY(session_id, event_id)
);

CREATE TABLE IF NOT EXISTS recognition_results (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES recognition_sessions(id) ON DELETE CASCADE,
    capture_timestamp TEXT NOT NULL,
    frame_sequence INTEGER,
    source_role TEXT,
    outcome TEXT NOT NULL
        CHECK (outcome IN ('recognized', 'unrecognized_face', 'no_face', 'processing_error')),
    identity_id INTEGER REFERENCES identities(id) ON DELETE SET NULL,
    similarity REAL,
    detection_confidence REAL,
    displayed_label TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_recognition_results_session_capture
    ON recognition_results(session_id, capture_timestamp);

CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES camera_events(id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES recognition_sessions(id) ON DELETE CASCADE,
    result_id INTEGER REFERENCES recognition_results(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('source_upload', 'face_crop', 'annotated_context')),
    relative_path TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (
        (event_id IS NOT NULL) + (session_id IS NOT NULL) + (result_id IS NOT NULL) = 1
    )
);

CREATE INDEX IF NOT EXISTS ix_screenshots_event ON screenshots(event_id);
CREATE INDEX IF NOT EXISTS ix_screenshots_session ON screenshots(session_id);
CREATE INDEX IF NOT EXISTS ix_screenshots_result ON screenshots(result_id);

CREATE TABLE IF NOT EXISTS event_dispatch (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL UNIQUE REFERENCES camera_events(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'claimed', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_event_dispatch_work
    ON event_dispatch(state, available_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS camera_event_tokens (
    id INTEGER PRIMARY KEY,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    token_hash BLOB NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_camera_event_tokens_camera
    ON camera_event_tokens(camera_id, revoked_at);
