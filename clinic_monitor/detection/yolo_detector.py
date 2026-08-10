"""
Stage 3 - object detection with YOLOv8n on CPU.

Only frames that passed the motion gate reach this stage, and only frames with
a confident person detection continue to Gemini.

Class coverage note
-------------------
The stock ``yolov8n.pt`` weights are trained on COCO, which contains
``person``, ``chair``, ``couch``, ``bed``, ``tv`` and ``laptop`` but has no
class for wheelchair, door or medical equipment. Those are reported as
"unsupported" rather than silently missing. Point ``CM_YOLO_MODEL`` at custom
weights and any extra classes they emit are passed through automatically.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import cv2
import numpy as np

import config

log = logging.getLogger(__name__)

# COCO name -> canonical label used by the rest of the system.
CLASS_ALIASES: Dict[str, str] = {
    "person": "person",
    "chair": "chair",
    "couch": "couch",
    "bed": "bed",
    "tv": "monitor",
    "laptop": "monitor",
    "dining table": "table",
    "bench": "bench",
}

# Requested by the spec but absent from COCO - needs custom weights.
UNSUPPORTED_CLASSES = ("wheelchair", "door", "medical equipment")

BOX_COLORS = {
    "person": (0, 200, 255),
    "chair": (180, 180, 180),
    "bed": (200, 120, 255),
    "monitor": (120, 255, 120),
}


@dataclass
class Detection:
    """One detected object."""

    label: str                       # canonical label ("person", "monitor", ...)
    raw_label: str                   # label as emitted by the model
    confidence: float
    box: Sequence[int]               # (x1, y1, x2, y2) in tile pixels

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "raw_label": self.raw_label,
            "confidence": round(float(self.confidence), 3),
            "box": [int(v) for v in self.box],
        }


class YoloDetector:
    """
    Thin, thread-safe wrapper around Ultralytics YOLOv8n.

    The model is loaded lazily so the rest of the pipeline (and the dashboard)
    can be imported without torch installed.
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        conf: Optional[float] = None,
        iou: Optional[float] = None,
        imgsz: Optional[int] = None,
        device: str = config.YOLO_DEVICE,
    ) -> None:
        self.model_path = model_path or config.YOLO_MODEL
        self.conf = conf if conf is not None else config.YOLO_CONF
        self.iou = iou if iou is not None else config.YOLO_IOU
        self.imgsz = imgsz or config.YOLO_IMGSZ
        self.device = device
        self._model = None
        self._names: Dict[int, str] = {}
        self._lock = threading.Lock()
        self.total_inferences = 0
        self.total_seconds = 0.0

    # -- loading ---------------------------------------------------------- #
    def load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch  # noqa: WPS433 - optional heavy import

            torch.set_num_threads(max(1, config.TORCH_THREADS))
        except Exception:  # pragma: no cover - torch is optional at import time
            log.debug("torch thread tuning skipped")

        try:
            from ultralytics import YOLO
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ultralytics is not installed - run "
                "`pip install -r requirements.txt`"
            ) from exc

        path = Path(self.model_path)
        log.info("loading YOLO model %s on %s", self.model_path, self.device)
        # Ultralytics downloads yolov8n.pt on first use if it is not present.
        self._model = YOLO(str(path) if path.exists() else self.model_path)
        self._model.to(self.device)
        self._names = dict(self._model.names)
        missing = [c for c in UNSUPPORTED_CLASSES if c not in self._names.values()]
        if missing:
            log.info(
                "model has no class for %s - those objects will not be reported",
                ", ".join(missing),
            )

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    # -- inference -------------------------------------------------------- #
    def detect(self, frame: np.ndarray) -> List[Detection]:
        """Run detection on a single tile. Returns [] on failure."""
        self.load()
        started = time.monotonic()
        try:
            with self._lock:  # ultralytics predict is not reentrant
                results = self._model.predict(
                    source=frame,
                    conf=self.conf,
                    iou=self.iou,
                    imgsz=self.imgsz,
                    device=self.device,
                    max_det=config.YOLO_MAX_DET,
                    verbose=False,
                )
        except Exception as exc:  # keep the pipeline alive
            log.error("YOLO inference failed: %s", exc)
            return []
        finally:
            self.total_seconds += time.monotonic() - started
            self.total_inferences += 1

        detections: List[Detection] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for box in boxes:
                cls_id = int(box.cls[0])
                raw = self._names.get(cls_id, str(cls_id))
                label = CLASS_ALIASES.get(raw, raw)
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                detections.append(
                    Detection(
                        label=label,
                        raw_label=raw,
                        confidence=float(box.conf[0]),
                        box=(x1, y1, x2, y2),
                    )
                )
        return detections

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def persons(
        detections: Sequence[Detection], min_conf: Optional[float] = None
    ) -> List[Detection]:
        threshold = min_conf if min_conf is not None else config.YOLO_PERSON_CONF
        return [
            d for d in detections if d.label == "person" and d.confidence >= threshold
        ]

    @classmethod
    def has_person(
        cls, detections: Sequence[Detection], min_conf: Optional[float] = None
    ) -> bool:
        return bool(cls.persons(detections, min_conf))

    @property
    def avg_inference_ms(self) -> float:
        if not self.total_inferences:
            return 0.0
        return round(1000.0 * self.total_seconds / self.total_inferences, 1)


def draw_detections(
    frame: np.ndarray, detections: Sequence[Detection], copy: bool = True
) -> np.ndarray:
    """Draw labelled boxes - used for the screenshots shown on the dashboard."""
    canvas = frame.copy() if copy else frame
    for det in detections:
        x1, y1, x2, y2 = det.box
        color = BOX_COLORS.get(det.label, (0, 165, 255))
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2)
        caption = f"{det.label} {det.confidence:.2f}"
        (tw, th), _ = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        cv2.rectangle(canvas, (x1, max(0, y1 - th - 6)), (x1 + tw + 6, y1), color, -1)
        cv2.putText(
            canvas,
            caption,
            (x1 + 3, max(10, y1 - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (20, 20, 20),
            1,
            cv2.LINE_AA,
        )
    return canvas
