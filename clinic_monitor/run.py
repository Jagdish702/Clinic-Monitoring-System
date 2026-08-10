"""
Plain-English front door to the monitoring system.

Say what you want in a sentence instead of assembling flags:

    python run.py "open hik connect app and open curebay nimapada camera feed
                   and analyze 1 min and tell me what is happening"

    python run.py                 # asks you for the instruction

The sentence is parsed locally - no API call is spent working out what you
meant - and the parse is printed before anything runs, so a misreading is
obvious rather than silent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ask  # noqa: E402

# "1 min", "90 seconds", "2 minutes", "30s"
_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?|[hms])\b",
    re.IGNORECASE,
)
# Clinic names in this deployment are all "CUREBAY <place>".
_CLINIC_RE = re.compile(r"cure\s*bay\s+([A-Za-z][A-Za-z0-9_-]{2,})", re.IGNORECASE)
# Or an explicitly quoted name, which always wins.
_QUOTED_RE = re.compile(r"[\"']([^\"']{3,60})[\"']")

_UNIT_SECONDS = {"h": 3600, "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
                 "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                 "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}

DEFAULT_DURATION = 60.0
LIST_WORDS = ("list clinic", "list the clinic", "which clinic", "what clinic",
              "show clinic", "all clinic", "list device")


def parse_duration(text: str) -> float:
    """Seconds to watch for. Defaults to a minute."""
    for value, unit in _DURATION_RE.findall(text):
        seconds = float(value) * _UNIT_SECONDS.get(unit.lower(), 1)
        # A bare number in a sentence is usually the duration, but guard
        # against nonsense like "analyse 0 min".
        if 5 <= seconds <= 3600:
            return seconds
    return DEFAULT_DURATION


def parse_clinic(text: str) -> Optional[str]:
    """The clinic to open. A quoted name wins over a 'curebay X' match."""
    quoted = _QUOTED_RE.search(text)
    if quoted:
        return quoted.group(1).strip()
    match = _CLINIC_RE.search(text)
    if match:
        return f"CUREBAY {match.group(1).upper()}"
    return None


def parse(text: str) -> Tuple[Optional[str], float, str]:
    """Return (clinic, seconds, question) for one instruction."""
    return parse_clinic(text), parse_duration(text), text.strip()


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    instruction = " ".join(argv).strip()
    if not instruction:
        print("What should I check? For example:")
        print('  open hik connect and check curebay banamalipur for 1 min, '
              "tell me what is happening")
        try:
            instruction = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not instruction:
        return 1

    lowered = instruction.lower()
    if any(word in lowered for word in LIST_WORDS):
        return ask.main(["--list-clinics"])

    clinic, seconds, question = parse(instruction)
    if not clinic:
        print(
            "\nI could not tell which clinic you mean. Name it as "
            '"CUREBAY <place>", for example:\n'
            '  python run.py "check curebay banamalipur for 1 minute"\n'
            "\nOr list what is available:\n"
            '  python run.py "list clinics"'
        )
        return 1

    print(f"\n  clinic   : {clinic}")
    print(f"  watch for: {seconds:.0f}s")
    print(f"  question : {question}\n")

    return ask.main(
        ["--open", clinic, "--duration", str(seconds), "--question", question]
    )


if __name__ == "__main__":
    raise SystemExit(main())
