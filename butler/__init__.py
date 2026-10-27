"""Local butler for the Discord Badge Maximizer.

The package re-uses the existing ``games.py`` and ``stream.py`` engines as
libraries, so there is still exactly one implementation of the telemetry and
voice/Go-Live protocol logic in this repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repository root (the folder that contains games.py / stream.py).
ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

__all__ = ["ROOT"]
