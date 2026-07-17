"""Enrollment application service; contains no HTTP concepts."""

from pathlib import Path
from uuid import uuid4
import cv2
import mediapipe as mp
import numpy as np

from .storage import EmbeddingStore
from .vision import process_frame


class EnrollmentError(Exception): pass


class EnrollmentService:
    def __init__(self, database, upload_dir, engine): self.database, self.upload_dir, self.engine = database, Path(upload_dir), engine
    def enroll(self, user, contents: bytes, suffix: str, original_name: str | None):
        frame = cv2.imdecode(np.frombuffer(contents, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None: raise EnrollmentError("The uploaded file is not a valid image.")
        detector = mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=.5)
        try: faces = process_frame(frame, detector, self.engine, {"detector": {"min_conf": .5, "pad_ratio": .15}})
        finally: detector.close()
        if not faces: raise EnrollmentError("No face was found. Try a clear, front-facing photo.")
        if len(faces) > 1: raise EnrollmentError("Multiple faces were found. Upload a photo with one person.")
        relative = Path(str(user.id)) / f"{uuid4().hex}{suffix}"; target = self.upload_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(contents)
        try:
            with EmbeddingStore(self.database) as store:
                identity = store.upsert_identity(f"user-{user.id}", user.display_name)
                embedding_id = store.add_embedding(identity, faces[0].embedding, source_path=relative,
                    detection_score=float(faces[0].score), metadata={"original_name": original_name})
        except Exception:
            target.unlink(missing_ok=True); raise
        return {"embeddingId": embedding_id, "message": "Face enrolled successfully.", "score": round(float(faces[0].score), 3)}
