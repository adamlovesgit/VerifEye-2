"""Enrollment application service; contains no HTTP concepts."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import cv2
import numpy as np

from .storage import EmbeddingStore
from .vision import create_face_detector, process_frame
from .vision.processing import annotate_frame, detect_faces, expand_box


class EnrollmentError(Exception): pass


class EnrollmentService:
    def __init__(self, database, upload_dir, engine): self.database, self.upload_dir, self.engine = database, Path(upload_dir), engine
    def enroll(self, display_name: str, contents: bytes, suffix: str, original_name: str | None):
        display_name = display_name.strip()
        if not display_name or len(display_name) > 100:
            raise EnrollmentError("Enter a name between 1 and 100 characters.")
        frame = cv2.imdecode(np.frombuffer(contents, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None: raise EnrollmentError("The uploaded file is not a valid image.")
        detector = create_face_detector(.5)
        try: faces = process_frame(frame, detector, self.engine, {"detector": {"min_conf": .5, "pad_ratio": .15}})
        finally: detector.close()
        if not faces: raise EnrollmentError("No face was found. Try a clear, front-facing photo.")
        if len(faces) > 1: raise EnrollmentError("Multiple faces were found. Upload a photo with one person.")
        relative = Path(f"{uuid4().hex}{suffix}"); target = self.upload_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(contents)
        try:
            with EmbeddingStore(self.database) as store:
                identity = store.upsert_identity(f"identity-{uuid4().hex}", display_name)
                try:
                    face = faces[0]
                    metadata = {"original_name": original_name}
                    if getattr(face, "raw_box", None) is not None:
                        metadata["raw_box"] = self._box_json(face.raw_box)
                    if getattr(face, "pad_box", None) is not None:
                        metadata["padded_box"] = self._box_json(face.pad_box)
                    if getattr(face, "landmarks", None) is not None:
                        metadata["landmarks"] = [[int(x), int(y)] for x, y in face.landmarks]
                    metadata["image_size"] = {"width": int(frame.shape[1]), "height": int(frame.shape[0])}
                    embedding_id = store.add_embedding(identity, face.embedding, source_path=relative,
                        detection_score=float(face.score), metadata=metadata)
                except Exception:
                    store.delete_identity(identity)
                    raise
        except Exception:
            target.unlink(missing_ok=True); raise
        return {"identityId": identity, "embeddingId": embedding_id, "displayName": display_name,
                "message": "Identity created successfully.", "score": round(float(faces[0].score), 3)}

    def reference_image(self, embedding_id: int) -> bytes:
        with EmbeddingStore(self.database) as store:
            record = store.get_embedding(embedding_id)
        if record is None or not record.source_path:
            raise KeyError(embedding_id)
        root = self.upload_dir.resolve()
        source = (root / record.source_path).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise FileNotFoundError(record.source_path)
        frame = cv2.imdecode(np.frombuffer(source.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise EnrollmentError("The stored enrollment image is invalid.")
        faces = self._stored_faces(record.metadata, record.detection_score)
        if not faces:
            detector = create_face_detector(.5)
            try:
                h, w = frame.shape[:2]
                faces = [SimpleNamespace(raw_box=box, pad_box=expand_box(box, .15, w, h),
                                         landmarks=landmarks, score=box.score)
                         for box, landmarks in detect_faces(frame, detector, .5)]
            finally:
                detector.close()
        annotated = annotate_frame(frame, faces, {})
        ok, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise EnrollmentError("The enrollment reference could not be rendered.")
        return encoded.tobytes()

    @staticmethod
    def _box_json(box) -> dict:
        return {"x1": int(box.x1), "y1": int(box.y1), "x2": int(box.x2), "y2": int(box.y2),
                "score": float(box.score)}

    @staticmethod
    def _stored_faces(metadata: dict, detection_score: float | None) -> list:
        raw, padded, landmarks = metadata.get("raw_box"), metadata.get("padded_box"), metadata.get("landmarks")
        if not raw or not padded or not landmarks:
            return []
        raw_box = SimpleNamespace(**raw)
        padded_box = SimpleNamespace(**padded)
        return [SimpleNamespace(raw_box=raw_box, pad_box=padded_box,
                                landmarks=[(int(x), int(y)) for x, y in landmarks],
                                score=float(detection_score if detection_score is not None else raw.get("score", 0)))]
