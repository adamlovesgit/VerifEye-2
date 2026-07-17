"""Run the complete recognition pipeline against one user-supplied image.

This is a manual integration test because it requires the MediaPipe detector and
the local InsightFace ArcFace model. It writes an annotated image for visual
inspection and a JSON report containing the numeric pipeline output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np


BACKEND_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = BACKEND_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from verifeye.vision import annotate_frame, process_frame  # noqa: E402


CONFIG = {
    "detector": {
        "model_selection": 0,
        "min_conf": 0.5,
        "pad_ratio": 0.15,
    }
}


def default_model_path() -> Path:
    return (
        Path.home()
        / ".insightface"
        / "models"
        / "buffalo_l"
        / "w600k_r50.onnx"
    )


def box_to_dict(box) -> dict[str, float | int]:
    return {
        "x1": box.x1,
        "y1": box.y1,
        "x2": box.x2,
        "y2": box.y2,
        "score": float(box.score),
    }


def face_to_dict(index: int, face) -> dict:
    embedding = np.asarray(face.embedding)
    return {
        "face_index": index,
        "score": float(face.score),
        "raw_box": box_to_dict(face.raw_box),
        "padded_box": box_to_dict(face.pad_box),
        "landmarks": [
            {"x": int(x), "y": int(y)} for x, y in face.landmarks
        ],
        "aligned_image": {
            "shape": list(face.aligned_rgb.shape),
            "dtype": str(face.aligned_rgb.dtype),
            "color_order": "RGB",
        },
        "embedding": {
            "shape": list(embedding.shape),
            "dtype": str(embedding.dtype),
            "l2_norm": float(np.linalg.norm(embedding)),
            "values": embedding.tolist(),
        },
    }


def validate_face(face, index: int) -> None:
    embedding = np.asarray(face.embedding)
    failures = []

    if face.aligned_rgb.shape != (112, 112, 3):
        failures.append(
            f"aligned image shape is {face.aligned_rgb.shape}, expected (112, 112, 3)"
        )
    if face.aligned_rgb.dtype != np.uint8:
        failures.append(
            f"aligned image dtype is {face.aligned_rgb.dtype}, expected uint8"
        )
    if embedding.shape != (512,):
        failures.append(f"embedding shape is {embedding.shape}, expected (512,)")
    if embedding.dtype != np.float32:
        failures.append(f"embedding dtype is {embedding.dtype}, expected float32")
    if not np.all(np.isfinite(embedding)):
        failures.append("embedding contains non-finite values")
    if not np.isclose(np.linalg.norm(embedding), 1.0, atol=1e-5):
        failures.append(
            f"embedding L2 norm is {np.linalg.norm(embedding):.8f}, expected 1.0"
        )

    if failures:
        details = "\n  - ".join(failures)
        raise AssertionError(f"Face {index} failed validation:\n  - {details}")


def run(input_path: Path, output_dir: Path, model_path: Path) -> tuple[Path, Path]:
    try:
        from insightface.model_zoo import get_model
    except ImportError as exc:
        raise RuntimeError(
            "InsightFace is required for this integration test; install the "
            "project's recognition dependencies first"
        ) from exc

    frame_bgr = cv2.imread(os.fspath(input_path), cv2.IMREAD_COLOR)
    if frame_bgr is None:
        raise ValueError(f"Could not decode input image: {input_path}")

    if not model_path.is_file():
        raise FileNotFoundError(
            f"ArcFace model not found at {model_path}. "
            "Pass its location with --model-path."
        )

    detector = mp.solutions.face_detection.FaceDetection(
        model_selection=CONFIG["detector"]["model_selection"],
        min_detection_confidence=CONFIG["detector"]["min_conf"],
    )
    try:
        recognizer = get_model(os.fspath(model_path))
        if recognizer is None:
            raise RuntimeError(f"Could not load ArcFace model at {model_path}")
        recognizer.prepare(ctx_id=-1)

        faces = process_frame(frame_bgr, detector, recognizer, CONFIG)
    finally:
        detector.close()

    if not faces:
        raise AssertionError("No face detected; use a clear image containing a face")

    for index, face in enumerate(faces):
        validate_face(face, index)

    annotated = annotate_frame(frame_bgr, faces, CONFIG)
    report = {
        "input_image": str(input_path.resolve()),
        "input_shape": list(frame_bgr.shape),
        "input_dtype": str(frame_bgr.dtype),
        "input_color_order": "BGR",
        "config": CONFIG,
        "face_count": len(faces),
        "faces": [face_to_dict(index, face) for index, face in enumerate(faces)],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    image_output = output_dir / f"{input_path.stem}_annotated.jpg"
    report_output = output_dir / f"{input_path.stem}_recognition.json"

    if not cv2.imwrite(os.fspath(image_output), annotated):
        raise OSError(f"Could not write annotated image: {image_output}")
    report_output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    return image_output, report_output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="Path to the user-supplied image")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=BACKEND_DIR / "test-output",
        help="Artifact directory (default: backend/test-output)",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=default_model_path(),
        help="Path to w600k_r50.onnx",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        image_output, report_output = run(
            args.image, args.output_dir, args.model_path
        )
    except (AssertionError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print("PASS: recognition pipeline produced valid face output")
    print(f"Annotated image: {image_output.resolve()}")
    print(f"Recognition data: {report_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
