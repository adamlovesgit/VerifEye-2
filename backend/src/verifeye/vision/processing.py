"""Pure image-processing operations for face recognition."""

import cv2
import numpy as np

from .types import BoundingBox, DetectedFace


def process_frame(frame_bgr, mp_ctx, rec, cfg):
    """Detect, align, and embed faces in the frame."""
    h, w = frame_bgr.shape[:2]
    detections = detect_faces(frame_bgr, mp_ctx, cfg["detector"]["min_conf"])
    faces = []

    for raw_box, landmarks in detections:
        pad_box = expand_box(raw_box, cfg["detector"]["pad_ratio"], w, h)
        aligned_rgb = align_face(frame_bgr, pad_box, landmarks, target_size=112)
        embedding = embed_face(aligned_rgb, rec)

        faces.append(
            DetectedFace(
                raw_box=raw_box,
                pad_box=pad_box,
                landmarks=landmarks,
                aligned_rgb=aligned_rgb,
                score=raw_box.score,
                embedding=embedding,
            )
        )

    return faces


def annotate_frame(frame_bgr, faces: list, cfg):
    """Draw boxes, landmarks, and scores on the image."""
    green = (0, 255, 0)
    red = (0, 0, 255)
    blue = (255, 0, 0)

    box_lines = 2
    lmk_rad = 3
    lmk_thick = -1
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.5
    font_thick = 1

    if not faces:
        return frame_bgr

    frame = frame_bgr.copy()

    for face in faces:
        r = face.raw_box
        p = face.pad_box
        f = face.score

        cv2.rectangle(frame, (r.x1, r.y1), (r.x2, r.y2), green, box_lines)
        cv2.putText(frame, f"{f:.2f}", (r.x1, r.y1 - 6), font, font_scale, green, font_thick)
        cv2.rectangle(frame, (p.x1, p.y1), (p.x2, p.y2), red, box_lines)

        for x, y in face.landmarks:
            cv2.circle(frame, (x, y), lmk_rad, blue, lmk_thick)

    return frame


def detect_faces(frame_bgr, mp_ctx, min_conf=0.5):
    """Return a list of ``(BoundingBox, landmarks[3])`` tuples."""
    h, w, _ = frame_bgr.shape
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    result = mp_ctx.process(frame_rgb)

    boxes_with_lmk = []
    if result.detections:
        for det in result.detections:
            score = det.score[0]
            if score < min_conf:
                continue

            bb = det.location_data.relative_bounding_box
            x1 = int(bb.xmin * w)
            y1 = int(bb.ymin * h)
            x2 = int((bb.xmin + bb.width) * w)
            y2 = int((bb.ymin + bb.height) * h)

            lmk_px = []
            rel_kp = det.location_data.relative_keypoints
            for kp in rel_kp[:3]:
                lmk_px.append((int(kp.x * w), int(kp.y * h)))

            boxes_with_lmk.append(
                (BoundingBox(x1, y1, x2, y2, score), lmk_px)
            )

    return boxes_with_lmk


def expand_box(box, pad_ratio, img_w, img_h):
    """Return a square, padded BoundingBox clamped to the image bounds."""
    w = box.x2 - box.x1
    h = box.y2 - box.y1
    cx = box.x1 + w / 2
    cy = box.y1 + h / 2

    new_w = w * (1 + pad_ratio)
    new_h = h * (1 + pad_ratio)

    side = max(new_w, new_h)
    new_w = new_h = side

    x1 = int(cx - new_w / 2)
    y1 = int(cy - new_h / 2)
    x2 = int(cx + new_w / 2)
    y2 = int(cy + new_h / 2)

    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(img_w - 1, x2), min(img_h - 1, y2)

    return BoundingBox(x1, y1, x2, y2, box.score)


def align_face(frame_bgr, pad_box, landmarks=None, target_size=112):
    """Return a cropped RGB face or align an RGB face using landmarks."""
    if landmarks is None or len(landmarks) < 3:
        crop_bgr = frame_bgr[pad_box.y1:pad_box.y2, pad_box.x1:pad_box.x2]
        frame_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        sized_rgb = cv2.resize(
            frame_rgb,
            (target_size, target_size),
            interpolation=cv2.INTER_LINEAR,
        )
        return sized_rgb

    src = np.array(landmarks[:3], dtype=np.float32)
    template = np.array(
        [
            [38.2946, 51.6963],
            [73.5318, 51.5014],
            [56.0252, 71.7366],
        ],
        dtype=np.float32,
    )

    scale = target_size / 112
    template *= scale

    matrix, _ = cv2.estimateAffinePartial2D(src, template, method=cv2.LMEDS)
    aligned_rgb = cv2.warpAffine(
        cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB),
        matrix,
        (target_size, target_size),
        flags=cv2.INTER_LINEAR,
    )

    return aligned_rgb


def embed_face(face_rgb, rec, target_size: int = 112):
    """Return a float32, L2-normalized embedding for an aligned RGB face."""
    if face_rgb.shape[:2] != (target_size, target_size):
        face_rgb = cv2.resize(face_rgb, (target_size, target_size), cv2.INTER_LINEAR)
    if face_rgb.dtype != np.uint8:
        face_rgb = face_rgb.astype(np.uint8)

    face_bgr = cv2.cvtColor(face_rgb, cv2.COLOR_RGB2BGR)
    emb = (
        rec.get_embedding(face_bgr)
        if hasattr(rec, "get_embedding")
        else rec.get_feat(face_bgr)
    )

    emb = np.asarray(emb, np.float32).reshape(-1)
    return emb / (np.linalg.norm(emb) + 1e-12)


def matching(emb1, emb2):
    """Return cosine similarity between two L2-normalized or raw vectors."""
    e1 = np.asarray(emb1, np.float32).reshape(-1)
    e2 = np.asarray(emb2, np.float32).reshape(-1)
    n1 = np.linalg.norm(e1)
    n2 = np.linalg.norm(e2)
    if n1 == 0 or n2 == 0:
        return 0.0
    e1 = e1 / n1
    e2 = e2 / n2
    return float(np.dot(e1, e2))
