"""Hosting-panel entry point (Pterodactyl / generic Linux container).

Panels that run ``python main.py`` as the startup command land here; the
arguments are forwarded to ``python -m butler``.
"""

from __future__ import annotations

from butler.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
