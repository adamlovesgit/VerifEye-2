"""SQLite-backed storage and cosine search for ArcFace embeddings."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

import numpy as np


DEFAULT_MODEL = "insightface/buffalo_l/w600k_r50"


@dataclass(frozen=True)
class EmbeddingRecord:
    id: int
    identity_id: int
    external_id: str
    display_name: str
    model_name: str
    embedding: np.ndarray
    source_path: str | None
    detection_score: float | None
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class Match:
    embedding_id: int
    identity_id: int
    external_id: str
    display_name: str
    similarity: float


class EmbeddingStore:
    """Own a SQLite connection and enforce the recognition vector contract."""

    def __init__(self, database: str | Path = "backend/data/verifeye.db") -> None:
        self.database = Path(database)
        if str(database) != ":memory:":
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        self._connection.executescript(schema)

    def __enter__(self) -> "EmbeddingStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def upsert_identity(self, external_id: str, display_name: str) -> int:
        if not external_id.strip() or not display_name.strip():
            raise ValueError("external_id and display_name must not be blank")
        with self._connection:
            self._connection.execute(
                """INSERT INTO identities(external_id, display_name)
                   VALUES (?, ?)
                   ON CONFLICT(external_id) DO UPDATE SET
                     display_name = excluded.display_name,
                     updated_at = CURRENT_TIMESTAMP""",
                (external_id, display_name),
            )
            row = self._connection.execute(
                "SELECT id FROM identities WHERE external_id = ?", (external_id,)
            ).fetchone()
        return int(row["id"])

    def add_embedding(
        self,
        identity_id: int,
        embedding: Iterable[float] | np.ndarray,
        *,
        model_name: str = DEFAULT_MODEL,
        source_path: str | Path | None = None,
        detection_score: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        vector = self._validate_embedding(embedding)
        if detection_score is not None and not 0.0 <= detection_score <= 1.0:
            raise ValueError("detection_score must be between 0 and 1")
        metadata_json = json.dumps(metadata or {}, separators=(",", ":"))
        with self._connection:
            cursor = self._connection.execute(
                """INSERT INTO face_embeddings(
                       identity_id, model_name, dimensions, dtype, vector, l2_norm,
                       source_path, detection_score, metadata_json
                   ) VALUES (?, ?, ?, 'float32', ?, ?, ?, ?, ?)""",
                (
                    identity_id,
                    model_name,
                    vector.size,
                    vector.tobytes(order="C"),
                    float(np.linalg.norm(vector)),
                    str(source_path) if source_path is not None else None,
                    detection_score,
                    metadata_json,
                ),
            )
        return int(cursor.lastrowid)

    def get_embedding(self, embedding_id: int) -> EmbeddingRecord | None:
        row = self._connection.execute(
            """SELECT e.*, i.external_id, i.display_name
               FROM face_embeddings e JOIN identities i ON i.id = e.identity_id
               WHERE e.id = ?""",
            (embedding_id,),
        ).fetchone()
        return self._record(row) if row else None

    def find_matches(
        self,
        embedding: Iterable[float] | np.ndarray,
        *,
        model_name: str = DEFAULT_MODEL,
        limit: int = 5,
        min_similarity: float = -1.0,
    ) -> list[Match]:
        query = self._validate_embedding(embedding)
        if limit < 1:
            raise ValueError("limit must be at least 1")
        rows = self._connection.execute(
            """SELECT e.id, e.identity_id, e.vector, i.external_id, i.display_name
               FROM face_embeddings e JOIN identities i ON i.id = e.identity_id
               WHERE e.model_name = ? AND e.dimensions = ?""",
            (model_name, query.size),
        ).fetchall()
        matches = []
        for row in rows:
            candidate = np.frombuffer(row["vector"], dtype=np.float32)
            similarity = float(np.dot(query, candidate))
            if similarity >= min_similarity:
                matches.append(
                    Match(row["id"], row["identity_id"], row["external_id"],
                          row["display_name"], similarity)
                )
        matches.sort(key=lambda match: match.similarity, reverse=True)
        return matches[:limit]

    @staticmethod
    def _validate_embedding(embedding: Iterable[float] | np.ndarray) -> np.ndarray:
        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if vector.size == 0 or not np.all(np.isfinite(vector)):
            raise ValueError("embedding must be a non-empty finite vector")
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-12:
            raise ValueError("embedding must have a non-zero norm")
        vector = np.ascontiguousarray(vector / norm, dtype=np.float32)
        vector.setflags(write=False)
        return vector

    @staticmethod
    def _record(row: sqlite3.Row) -> EmbeddingRecord:
        vector = np.frombuffer(row["vector"], dtype=np.float32).copy()
        return EmbeddingRecord(
            id=row["id"], identity_id=row["identity_id"],
            external_id=row["external_id"], display_name=row["display_name"],
            model_name=row["model_name"], embedding=vector,
            source_path=row["source_path"], detection_score=row["detection_score"],
            metadata=json.loads(row["metadata_json"]), created_at=row["created_at"],
        )
