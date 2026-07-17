PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO schema_version(version) VALUES (1);

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
