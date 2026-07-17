"""Opt-in, non-recording real RTSP recognition smoke test."""

import argparse
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import cv2
import mediapipe as mp
from verifeye.cameras.runtime import FramePublisher, PyAvFrameSource
from verifeye.recognition import IdentityMatcher, RecognitionEngine, annotate
from verifeye.vision import process_frame


def main():
    parser=argparse.ArgumentParser(description="Verify a real RTSP camera without recording media.")
    parser.add_argument("--url", default=os.getenv("VERIFEYE_TEST_RTSP_URL")); parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--database", default="backend/data/verifeye.db"); parser.add_argument("--model-path", default=os.getenv("VERIFEYE_MODEL_PATH", str(Path.home()/".insightface/models/buffalo_l/w600k_r50.onnx")))
    args=parser.parse_args()
    if not args.url: parser.error("Provide --url or VERIFEYE_TEST_RTSP_URL.")
    engine=RecognitionEngine(Path(args.model_path)); matcher=IdentityMatcher(args.database,.40); source=None; detector=None; publisher=FramePublisher(); frames=faces=0
    try:
        source=PyAvFrameSource(args.url,8); detector=mp.solutions.face_detection.FaceDetection(model_selection=0,min_detection_confidence=.5); deadline=time.monotonic()+args.seconds
        for frame in source.frames():
            detected=process_frame(frame,detector,engine,{"detector":{"min_conf":.5,"pad_ratio":.15}}); labeled=[(face,matcher.match(face.embedding)) for face in detected]
            ok,jpeg=cv2.imencode(".jpg",annotate(frame,labeled));
            if ok: publisher.publish(jpeg.tobytes()); sequence,a=publisher.wait_after(0,0); _,b=publisher.wait_after(0,0); assert sequence and a==b
            frames+=1; faces+=len(detected)
            if time.monotonic()>=deadline: break
        if not frames: raise RuntimeError("The camera produced no frames.")
        print(f"OK: decoded {frames} frames and recognized {faces} face samples; fan-out was non-destructive.")
    finally:
        if source: source.close()
        if detector: detector.close()
        publisher.close(); engine.close()

if __name__=="__main__": main()
