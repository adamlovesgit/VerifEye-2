"""MediaPipe face-detector construction across legacy and Tasks APIs."""
from pathlib import Path
from types import SimpleNamespace
import mediapipe as mp

DEFAULT_MODEL = Path(__file__).resolve().parents[3] / "models" / "blaze_face_short_range.tflite"

class TasksFaceDetectorAdapter:
    def __init__(self, detector): self._detector = detector
    def process(self, frame_rgb):
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
        result = self._detector.detect(image)
        detections = []
        for item in result.detections:
            box = item.bounding_box
            h, w = frame_rgb.shape[:2]
            relative_box = SimpleNamespace(xmin=box.origin_x/w, ymin=box.origin_y/h, width=box.width/w, height=box.height/h)
            location = SimpleNamespace(relative_bounding_box=relative_box, relative_keypoints=item.keypoints)
            detections.append(SimpleNamespace(score=[item.categories[0].score], location_data=location))
        return SimpleNamespace(detections=detections)
    def __enter__(self): return self
    def __exit__(self, exc_type, exc_value, traceback): self.close()
    def close(self): self._detector.close()

def create_face_detector(min_confidence: float = 0.5, model_path=DEFAULT_MODEL):
    if hasattr(mp, "solutions"):
        return mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=min_confidence)
    model_path = Path(model_path)
    if not model_path.is_file(): raise RuntimeError(f"MediaPipe face detector model is missing: {model_path}")
    options = mp.tasks.vision.FaceDetectorOptions(base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)), min_detection_confidence=min_confidence)
    return TasksFaceDetectorAdapter(mp.tasks.vision.FaceDetector.create_from_options(options))
