"""Face-recognition data types and pure image-processing operations."""

from .detector import create_face_detector

from .processing import (
    align_face,
    annotate_frame,
    detect_faces,
    embed_face,
    expand_box,
    matching,
    process_frame,
)
from .types import BoundingBox, DetectedFace

__all__ = [
    "BoundingBox",
    "DetectedFace",
    "align_face",
    "annotate_frame",
    "create_face_detector",
    "detect_faces",
    "embed_face",
    "expand_box",
    "matching",
    "process_frame",
]
