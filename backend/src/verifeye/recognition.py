"""Shared recognition resources and framework-independent matching."""

from __future__ import annotations

from dataclasses import dataclass
import threading

import cv2
import numpy as np

from .storage import EmbeddingStore
from .vision.processing import embed_face


@dataclass(frozen=True)
class RecognitionLabel:
    display_name: str
    similarity: float | None


class RecognitionEngine:
    """Sole owner of the ArcFace model and serialized model inference."""

    def __init__(self, model_path) -> None:
        from insightface.model_zoo import get_model
        if not model_path.is_file():
            raise RuntimeError(f"Recognition model is not installed at {model_path}")
        self._model = get_model(str(model_path))
        if self._model is None:
            raise RuntimeError("Recognition model could not be loaded.")
        self._model.prepare(ctx_id=-1)
        self._lock = threading.Lock()
        self._closed = False

    def embedding(self, aligned_rgb) -> np.ndarray:
        with self._lock:
            if self._closed:
                raise RuntimeError("Recognition engine is closed.")
            return embed_face(aligned_rgb, self._model)

    def get_embedding(self, face_bgr) -> np.ndarray:
        """Compatibility port used by the pure frame pipeline."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Recognition engine is closed.")
            value = self._model.get_embedding(face_bgr) if hasattr(self._model, "get_embedding") else self._model.get_feat(face_bgr)
        value = np.asarray(value, np.float32).reshape(-1)
        return value / (np.linalg.norm(value) + 1e-12)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._model = None


class IdentityMatcher:
    def __init__(self, database, threshold: float) -> None:
        self.database, self.threshold = database, threshold

    def match(self, embedding) -> RecognitionLabel:
        with EmbeddingStore(self.database) as store:
            matches = store.find_matches(embedding, limit=1, min_similarity=self.threshold)
        if not matches:
            return RecognitionLabel("Unknown", None)
        return RecognitionLabel(matches[0].display_name, matches[0].similarity)


def annotate(frame, faces_and_labels) -> np.ndarray:
    output = frame.copy()
    for face, label in faces_and_labels:
        box = face.raw_box
        color = (40, 190, 90) if label.display_name != "Unknown" else (50, 140, 240)
        cv2.rectangle(output, (box.x1, box.y1), (box.x2, box.y2), color, 2)
        caption = label.display_name
        if label.similarity is not None:
            caption += f" {label.similarity:.2f}"
        cv2.putText(output, caption, (box.x1, max(20, box.y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, .55, color, 2)
    return output
