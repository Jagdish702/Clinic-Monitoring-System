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

from collections import defaultdict
from typing import Dict, Iterable, Optional, Tuple

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


def infer_roles(
    rows: Iterable[dict], min_agreement: float = 0.6, min_samples: int = 2
) -> Dict[Tuple[str, str], str]:
    """
    Map (clinic, camera) -> "indoor" / "outdoor" from stored descriptions.

    A camera is only assigned a role when the descriptions agree; a single
    ambiguous sighting is left unclassified rather than guessed at, because a
    wrong role would suppress a status pill that was actually meaningful.
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
    return roles


def indoor_cameras(roles: Dict[Tuple[str, str], str], clinic: str) -> set:
    """Cameras at one clinic known to look indoors."""
    return {
        camera
        for (name, camera), role in roles.items()
        if name == clinic and role == INDOOR
    }
