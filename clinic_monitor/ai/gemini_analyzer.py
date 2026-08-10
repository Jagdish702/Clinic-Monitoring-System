"""
Stage 4 - scene understanding with Gemini 2.0 Flash.

Only frames that passed motion detection *and* contain a confident person
detection get here, and even then three throttles apply:

1. per-camera cooldown  (``GEMINI_COOLDOWN_SEC``)
2. global hourly ceiling (``GEMINI_MAX_CALLS_PER_HOUR``)
3. a circuit breaker that pauses calls after repeated API failures

The analyzer never raises into the pipeline: on any failure it returns ``None``
and the caller logs a degraded, YOLO-only event instead.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

import config

log = logging.getLogger(__name__)

SEVERITIES = ("Low", "Medium", "High")


class TruncatedResponse(RuntimeError):
    """The model ran out of output tokens, so the JSON is incomplete."""


def _was_truncated(response: Any) -> bool:
    for candidate in getattr(response, "candidates", None) or []:
        reason = str(getattr(candidate, "finish_reason", "") or "")
        if "MAX_TOKENS" in reason.upper():
            return True
    return False

PROMPT = """You are a clinic monitoring assistant reviewing a single still frame
from a CCTV feed shown inside the Hik-Connect app.

Clinic: {clinic}
Camera: {camera}
Local time: {timestamp}
On-device detector found: {detections}

Look at the image and answer:
- What is happening?
- Is the clinic open?
- Is staff present?
- Is a patient visible?
- Is anything unusual happening?
- Is immediate attention required?

Rules:
- Judge only what is visible. Do not speculate about identities.
- The image is a phone screenshot of a CCTV grid, so it may be low quality.
- Severity: "Low" = normal activity; "Medium" = something worth a look
  (crowding, long wait, someone unattended, after-hours presence);
  "High" = possible emergency (fall, collapse, fight, fire, theft, medical
  distress) or anything needing immediate human attention.
- If the view is unclear or empty, use severity "Low" and say so.

Return JSON only, no markdown, exactly these keys:
{{
  "description": "<one sentence summary>",
  "clinic_status": "Open" | "Closed" | "Unclear",
  "staff_present": true | false,
  "patient_present": true | false,
  "unusual_activity": true | false,
  "immediate_attention": true | false,
  "severity": "Low" | "Medium" | "High",
  "reason": "<short justification>"
}}{extra}"""

# Appended when the caller asks something specific on top of the standard fields.
EXTRA_QUESTION_BLOCK = """

The operator also asks: "{question}"
Add one more key to the JSON:
  "answer": "<direct answer to that question, or 'cannot tell from this view'>"
"""


@dataclass
class SceneAnalysis:
    """Structured result returned by Gemini."""

    description: str
    clinic_status: str = "Unclear"
    severity: str = "Low"
    reason: str = ""
    staff_present: bool = False
    patient_present: bool = False
    unusual_activity: bool = False
    immediate_attention: bool = False
    answer: str = ""            # only set when the caller passed a question
    model: str = ""
    latency_ms: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "description": self.description,
            "clinic_status": self.clinic_status,
            "severity": self.severity,
            "reason": self.reason,
            "staff_present": self.staff_present,
            "patient_present": self.patient_present,
            "unusual_activity": self.unusual_activity,
            "immediate_attention": self.immediate_attention,
            "answer": self.answer,
            "model": self.model,
            "latency_ms": self.latency_ms,
        }


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "y"}
    return bool(value)


def _normalise_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    for sev in SEVERITIES:
        if text == sev.lower():
            return sev
    if text in {"critical", "urgent", "severe"}:
        return "High"
    if text in {"moderate", "warning"}:
        return "Medium"
    return "Low"


def _normalise_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("open"):
        return "Open"
    if text.startswith("clos"):
        return "Closed"
    return "Unclear"


def parse_response(text: str) -> Optional[Dict[str, Any]]:
    """Pull a JSON object out of a model response, tolerating stray prose."""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


class RateLimiter:
    """Sliding-window limiter shared by every clinic."""

    def __init__(self, max_calls: int, window_sec: float = 3600.0) -> None:
        self.max_calls = max_calls
        self.window_sec = window_sec
        self._calls: Deque[float] = deque()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._calls and now - self._calls[0] > self.window_sec:
                self._calls.popleft()
            if self.max_calls and len(self._calls) >= self.max_calls:
                return False
            self._calls.append(now)
            return True

    @property
    def used(self) -> int:
        now = time.monotonic()
        with self._lock:
            while self._calls and now - self._calls[0] > self.window_sec:
                self._calls.popleft()
            return len(self._calls)


class GeminiAnalyzer:
    """Vision analysis with cooldowns, rate limiting and a circuit breaker."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        enabled: Optional[bool] = None,
        cooldown_sec: Optional[float] = None,
        max_calls_per_hour: Optional[int] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else config.GEMINI_API_KEY
        self.model = model or config.GEMINI_MODEL
        self.cooldown_sec = (
            cooldown_sec if cooldown_sec is not None else config.GEMINI_COOLDOWN_SEC
        )
        self.enabled = config.GEMINI_ENABLED if enabled is None else enabled
        if self.enabled and not self.api_key:
            log.warning(
                "GEMINI_API_KEY is not set - stage 4 disabled, events will be "
                "logged from YOLO only"
            )
            self.enabled = False

        self._limiter = RateLimiter(
            max_calls_per_hour
            if max_calls_per_hour is not None
            else config.GEMINI_MAX_CALLS_PER_HOUR
        )
        self._client = None
        self._client_lock = threading.Lock()
        self._last_call: Dict[Tuple[str, str], float] = {}
        self._cooldown_lock = threading.Lock()
        self._consecutive_failures = 0
        self._blocked_until = 0.0
        self._thinking_mode = "budget"  # negotiated on the first call

        self.calls_made = 0
        self.calls_failed = 0
        self.calls_skipped_cooldown = 0
        self.calls_skipped_quota = 0

    # -- client ----------------------------------------------------------- #
    def _get_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                from google import genai  # imported lazily

                self._client = genai.Client(api_key=self.api_key)
        return self._client

    # -- throttling ------------------------------------------------------- #
    def should_analyze(self, clinic: str, camera: str) -> bool:
        """True when a call for this camera is allowed right now."""
        if not self.enabled:
            return False
        now = time.monotonic()
        if now < self._blocked_until:
            return False
        key = (clinic, camera)
        with self._cooldown_lock:
            last = self._last_call.get(key, 0.0)
            if now - last < self.cooldown_sec:
                self.calls_skipped_cooldown += 1
                return False
        return True

    def _mark_called(self, clinic: str, camera: str) -> None:
        with self._cooldown_lock:
            self._last_call[(clinic, camera)] = time.monotonic()

    def _record_failure(self) -> None:
        self.calls_failed += 1
        self._consecutive_failures += 1
        if self._consecutive_failures >= config.GEMINI_FAILURE_THRESHOLD:
            self._trip_breaker(f"{self._consecutive_failures} failures in a row")
            self._consecutive_failures = 0

    def _trip_breaker(self, why: str, seconds: Optional[float] = None) -> None:
        """Pause stage 4 for a while; the pipeline keeps running without it."""
        pause = seconds if seconds is not None else config.GEMINI_FAILURE_BACKOFF_SEC
        self._blocked_until = time.monotonic() + pause
        log.error("pausing Gemini for %.0fs (%s)", pause, why)

    # -- image prep ------------------------------------------------------- #
    @staticmethod
    def encode_frame(frame: np.ndarray) -> Optional[bytes]:
        h, w = frame.shape[:2]
        if w > config.GEMINI_MAX_WIDTH:
            scale = config.GEMINI_MAX_WIDTH / float(w)
            frame = cv2.resize(
                frame,
                (config.GEMINI_MAX_WIDTH, max(1, int(round(h * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        ok, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), config.GEMINI_JPEG_QUALITY]
        )
        return buf.tobytes() if ok else None

    # -- main entry point ------------------------------------------------- #
    def analyze(
        self,
        frame: np.ndarray,
        clinic_name: str,
        camera_name: str,
        detections: Optional[Sequence[Any]] = None,
        timestamp: Optional[str] = None,
        extra_question: Optional[str] = None,
    ) -> Optional[SceneAnalysis]:
        """
        Describe the scene. Returns ``None`` when skipped or on any failure -
        the pipeline must keep running regardless.
        """
        if not self.should_analyze(clinic_name, camera_name):
            return None
        if not self._limiter.try_acquire():
            self.calls_skipped_quota += 1
            log.debug("hourly Gemini quota reached - skipping %s/%s", clinic_name, camera_name)
            return None

        payload = self.encode_frame(frame)
        if payload is None:
            log.error("could not JPEG-encode the frame for Gemini")
            return None

        summary = _summarise_detections(detections)
        prompt = PROMPT.format(
            clinic=clinic_name,
            camera=camera_name,
            timestamp=timestamp or time.strftime("%Y-%m-%d %H:%M:%S"),
            detections=summary,
            extra=(
                EXTRA_QUESTION_BLOCK.format(question=extra_question.strip())
                if extra_question
                else ""
            ),
        )

        self._mark_called(clinic_name, camera_name)
        started = time.monotonic()

        for attempt in range(config.GEMINI_MAX_RETRIES + 1):
            try:
                text = self._generate(payload, prompt)
            except Exception as exc:
                # Quota/rate-limit errors will not resolve within a retry
                # window, so trip the breaker instead of burning more calls.
                if isinstance(exc, TruncatedResponse):
                    # Retrying with identical settings would truncate again.
                    self.calls_failed += 1
                    log.error("Gemini response truncated: %s", exc)
                    return None
                if _is_quota_error(exc):
                    self.calls_failed += 1
                    log.error("Gemini quota exhausted: %s", _summarise_error(exc))
                    self._trip_breaker(
                        "quota exhausted", _retry_delay_seconds(exc)
                    )
                    return None
                if attempt < config.GEMINI_MAX_RETRIES:
                    delay = config.GEMINI_RETRY_BACKOFF_SEC * (attempt + 1)
                    log.warning(
                        "Gemini call failed (%s) - retrying in %.0fs",
                        _summarise_error(exc),
                        delay,
                    )
                    time.sleep(delay)
                    continue
                log.error(
                    "Gemini call failed after retries: %s", _summarise_error(exc)
                )
                self._record_failure()
                return None

            data = parse_response(text)
            if data is None:
                if attempt < config.GEMINI_MAX_RETRIES:
                    log.warning("Gemini returned unparsable output - retrying")
                    continue
                log.error("Gemini returned unparsable output: %.200s", text)
                self._record_failure()
                return None

            self.calls_made += 1
            self._consecutive_failures = 0
            return self._to_analysis(data, int((time.monotonic() - started) * 1000))

        return None

    # Flash models reason before answering, which slows the call down and eats
    # the output budget - a truncated reply is unparsable JSON. Scene
    # description needs no reasoning, but every model generation spells the
    # "off" switch differently, so try the options in order and remember the
    # first one the model accepts.
    def _thinking_config(self, types):
        if not config.GEMINI_DISABLE_THINKING or not hasattr(types, "ThinkingConfig"):
            return None
        try:
            if self._thinking_mode == "budget":
                return types.ThinkingConfig(thinking_budget=0)
            if self._thinking_mode == "level":
                return types.ThinkingConfig(thinking_level="low")
        except Exception:  # field not present in this SDK version
            self._thinking_mode = "off"
        return None

    def _downgrade_thinking(self) -> bool:
        """Step to the next thinking setting. False when out of options."""
        order = ("budget", "level", "off")
        try:
            nxt = order[order.index(self._thinking_mode) + 1]
        except (ValueError, IndexError):
            return False
        self._thinking_mode = nxt
        return True

    def _generate(self, image_bytes: bytes, prompt: str) -> str:
        from google.genai import types

        while True:
            try:
                return self._generate_once(types, image_bytes, prompt)
            except Exception as exc:
                if _is_invalid_argument(exc) and self._downgrade_thinking():
                    log.warning(
                        "%s rejected the thinking setting - falling back to %r",
                        self.model,
                        self._thinking_mode,
                    )
                    continue
                raise

    def _generate_once(self, types, image_bytes: bytes, prompt: str) -> str:
        settings = dict(
            temperature=0.2,
            max_output_tokens=config.GEMINI_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
        )
        thinking = self._thinking_config(types)
        if thinking is not None:
            settings["thinking_config"] = thinking

        response = self._get_client().models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt,
            ],
            config=types.GenerateContentConfig(**settings),
        )

        if _was_truncated(response):
            raise TruncatedResponse(
                "the model hit max_output_tokens - raise CM_GEMINI_MAX_OUTPUT_TOKENS"
            )
        return (response.text or "").strip()

    def _to_analysis(self, data: Dict[str, Any], latency_ms: int) -> SceneAnalysis:
        severity = _normalise_severity(data.get("severity"))
        immediate = _coerce_bool(data.get("immediate_attention"))
        if immediate and severity != "High":
            severity = "High"  # keep the two fields consistent
        return SceneAnalysis(
            description=str(data.get("description") or "No description returned.").strip(),
            clinic_status=_normalise_status(data.get("clinic_status")),
            severity=severity,
            reason=str(data.get("reason") or "").strip(),
            staff_present=_coerce_bool(data.get("staff_present")),
            patient_present=_coerce_bool(data.get("patient_present")),
            unusual_activity=_coerce_bool(data.get("unusual_activity")),
            immediate_attention=immediate,
            answer=str(data.get("answer") or "").strip(),
            model=self.model,
            latency_ms=latency_ms,
            raw=data,
        )

    # -- diagnostics ------------------------------------------------------ #
    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "calls_made": self.calls_made,
            "calls_failed": self.calls_failed,
            "skipped_cooldown": self.calls_skipped_cooldown,
            "skipped_quota": self.calls_skipped_quota,
            "used_this_hour": self._limiter.used,
            "paused": time.monotonic() < self._blocked_until,
        }


def _is_quota_error(exc: Exception) -> bool:
    """429 / RESOURCE_EXHAUSTED - retrying inside this call cannot help."""
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text.split("{", 1)[0]


def _is_invalid_argument(exc: Exception) -> bool:
    """400 - usually a config field this model does not accept."""
    text = str(exc)
    return "INVALID_ARGUMENT" in text or text.lstrip().startswith("400")


def _retry_delay_seconds(exc: Exception) -> Optional[float]:
    """Honour the API's own retryDelay hint when it sends one."""
    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", str(exc))
    if not match:
        match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(exc))
    if not match:
        return None
    # A little headroom on top of what the API asked for.
    return min(600.0, float(match.group(1)) + 5.0)


def _summarise_error(exc: Exception) -> str:
    """One readable line - the raw 429 payload is several KB of JSON."""
    text = " ".join(str(exc).split())
    match = re.search(r"'message':\s*'([^']{0,180})", text)
    if match:
        head = text.split("{", 1)[0].strip().rstrip(".")
        return f"{head}: {match.group(1)}"
    return text[:200]


def _summarise_detections(detections: Optional[Sequence[Any]]) -> str:
    if not detections:
        return "nothing"
    counts: Dict[str, int] = {}
    for det in detections:
        label = getattr(det, "label", None) or str(det)
        counts[label] = counts.get(label, 0) + 1
    return ", ".join(f"{count} x {label}" for label, count in sorted(counts.items()))
