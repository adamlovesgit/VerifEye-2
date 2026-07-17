"""Data types used by the face-recognition pipeline."""

from collections import namedtuple


BoundingBox = namedtuple("BoundingBox", ["x1", "y1", "x2", "y2", "score"])


class DetectedFace:
    """Container for detected face information."""

    def __init__(self, raw_box, pad_box, landmarks, aligned_rgb, score, embedding):
        self.raw_box = raw_box
        self.pad_box = pad_box
        self.landmarks = landmarks
        self.aligned_rgb = aligned_rgb
        self.score = score
        self.embedding = embedding
