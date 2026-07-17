"""Local persistence for identities and face embeddings."""

from .embedding_store import EmbeddingRecord, EmbeddingStore, Match

__all__ = ["EmbeddingRecord", "EmbeddingStore", "Match"]
