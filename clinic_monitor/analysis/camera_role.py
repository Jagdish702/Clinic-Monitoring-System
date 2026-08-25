"""
Work out which camera at a clinic looks indoors and which looks outdoors.

This matters because the two answer different questions. Measured across this
deployment, at the same clinic and the same minute:

    ASTARANGA   18:00   Camera 01 = Closed   Camera 02 = Open
    NAYAHAT     19:00   Camera 01 = Closed   Camera 02 = Open
    BAMANAL     18:00   Camera 01 = Closed   Camera 02 = Open
    BANAMALIPUR 18:00   Camera 01 = Open     Camera 02 = Closed   (cameras reversed)

In every case the *outdoor* camera reported "Closed" and the *indoor* camera
reported "Open". An empty street in the evening looks shut whether or not the
clinic is working, so an outdoor view cannot answer "is the clinic open?" - it
is answering a different question and its verdict is systematically pessimistic.

Roles are inferred from the scene descriptions already stored rather than
configured by hand, so they need no per-clinic setup and improve as more
observations accumulate. Note the layout is *not* consistent: most clinics
have Camera 01 outdoors, but BANAMALIPUR and DELANGA are the other way round,
which is exactly why this is derived rather than assumed.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import config

log = logging.getLogger(__name__)

INDOOR = "indoor"
OUTDOOR = "outdoor"

_OUTDOOR_WORDS = (
    "outdoor", "outside", "entrance", "walkway", "corridor", "street", "road",
    "parked", "motorcycle", "motorbike", "scooter", "bicycle", "vehicle",
    "courtyard", "porch", "exterior", "alley", "pavement", "gate", "veranda",
    "cattle", "cow", "footwear", "premises",
)
_INDOOR_WORDS = (
    "reception", "consultation", "consulting", "examination", "interior",
    "indoor", "inside the clinic", "desk", "waiting", "ward", "counter",
    "cabin", "curtain", "shelves", "medicine", "pharmacy", "stool",
    "clinic room", "consultation room",
)

# Descriptions the pipeline writes itself, which describe no scene at all.
_NON_SCENE = (
    "no activity:", "camera fault:", "no signal", "hikvision logo",
    "black screen", "no video", "blank",
)


def classify_description(text: Optional[str]) -> Optional[str]:
    """Indoor or outdoor for one description, or None when it says neither."""
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in _NON_SCENE):
        return None
    outdoor = sum(1 for word in _OUTDOOR_WORDS if word in lowered)
    indoor = sum(1 for word in _INDOOR_WORDS if word in lowered)
    if outdoor > indoor:
        return OUTDOOR
    if indoor > outdoor:
        return INDOOR
    return None


def load_overrides(path: Optional[Path] = None) -> Dict[Tuple[str, str], str]:
    """
    Roles set by hand, which always beat what the descriptions suggest.

    Reading the scene only tells us whether a camera points at an interior,
    and that is not quite the question. At Gop both cameras are indoors, but
    one watches the reception area and the other the consulting room - and
    only the consulting room answers "is the clinic working?". No wording in
    the description distinguishes them, so somebody who knows the site has to
    say. The file is plain JSON so that can be done without a code change:

        {"CureBay Gop": {"Camera 01": "outdoor", "Camera 02": "indoor"}}
    """
    path = Path(path or config.CAMERA_ROLES_PATH)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read camera roles from %s: %s", path, exc)
        return {}

    overrides: Dict[Tuple[str, str], str] = {}
    for clinic, cameras in (raw or {}).items():
        # The file carries its own instructions under "_comment"; skip that and
        # anything else that is not a clinic -> {camera: role} mapping, so a
        # typo cannot take the whole file down with it.
        if clinic.startswith("_") or not isinstance(cameras, dict):
            continue
        for camera, role in cameras.items():
            role = str(role).strip().lower()
            if role not in (INDOOR, OUTDOOR):
                log.warning("ignoring role %r for %s/%s - expected %r or %r",
                            role, clinic, camera, INDOOR, OUTDOOR)
                continue
            overrides[(clinic.strip(), camera.strip())] = role
    return overrides


def infer_roles(
    rows: Iterable[dict], min_agreement: float = 0.6, min_samples: int = 2
) -> Dict[Tuple[str, str], str]:
    """
    Map (clinic, camera) -> "indoor" / "outdoor" from stored descriptions.

    A camera is only assigned a role when the descriptions agree; a single
    ambiguous sighting is left unclassified rather than guessed at, because a
    wrong role would suppress a status pill that was actually meaningful.

    Anything named in the overrides file wins outright - see load_overrides.
    """
    tally: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(
        lambda: {INDOOR: 0, OUTDOOR: 0}
    )
    for row in rows:
        role = classify_description(row.get("description"))
        if role:
            tally[(row["clinic_name"], row["camera_name"])][role] += 1

    roles: Dict[Tuple[str, str], str] = {}
    for key, counts in tally.items():
        total = counts[INDOOR] + counts[OUTDOOR]
        if total < min_samples:
            continue
        winner = INDOOR if counts[INDOOR] >= counts[OUTDOOR] else OUTDOOR
        if counts[winner] / total >= min_agreement:
            roles[key] = winner

    # Applied last, and to every camera named - including ones the
    # descriptions never managed to classify at all. Names are matched without
    # regard to case, because the clinic is written "CureBay Gop" in some
    # places and "CUREBAY GOP" in others and nobody should have to guess which.
    for (clinic, camera), role in load_overrides().items():
        wanted = (clinic.lower(), camera.lower())
        matched = [
            key for key in roles
            if (key[0].strip().lower(), key[1].strip().lower()) == wanted
        ]
        for key in matched:
            roles[key] = role
        if not matched:
            roles[(clinic, camera)] = role
    return roles


def indoor_cameras(roles: Dict[Tuple[str, str], str], clinic: str) -> set:
    """Cameras at one clinic known to look indoors."""
    return {
        camera
        for (name, camera), role in roles.items()
        if name == clinic and role == INDOOR
    }
