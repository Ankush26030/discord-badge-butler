"""Interactive credential entry (run locally, never through Discord).

    python -m butler auth

Writes the account token, cf_clearance cookie and (optionally) a warm analytics
token into the shared ``session.json`` that ``games.py`` / ``stream.py`` use.
"""

from __future__ import annotations

import getpass
import sys

import games as games_engine

from .bridge import ButlerError, verify_user_token

BANNER = """
  ────────────────────────────────────────────────
   butler · local credential setup
  ────────────────────────────────────────────────
  This prompt only writes to session.json on this PC.
  Never paste these values into Discord chat.
"""


def _read_secret(prompt: str) -> str:
    """Hidden input on a real console, plain input when piped/redirected."""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return getpass.getpass(prompt)
    except Exception:  # noqa: BLE001 - fall back to visible input
        pass
    return input(prompt)


def run() -> int:
    print(BANNER)
    state = games_engine.AccountSessionState.restore()

    if state.token:
        print(f"  token already saved (user id {games_engine.extract_account_snowflake(state.token) or '?'})")
    else:
        print("  no token saved yet")

    try:
        token = _read_secret("  Discord account token (hidden, empty = keep): ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n  cancelled")
        return 1

    if token:
        try:
            user = verify_user_token(token)
        except ButlerError as exc:
            print(f"  ✗ {exc}")
            return 1
        print(f"  ✓ token valid for {user.get('username', '?')} (id {user.get('id', '?')})")
        state.token = token
        state.analytics_token = ""
        state.fetched_at = 0
        state.persist()

    if not state.token:
        print("  ✗ a token is required")
        return 1

    cookie_default = " (empty = keep current)" if state.cookie else ""
    try:
        cookie = input(f"  cf_clearance cookie{cookie_default}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n  cancelled")
        return 1

    if cookie:
        state.cookie = games_engine.normalize_clearance_cookie(cookie)
        state.persist()
        print("  ✓ cookie saved")

    if not state.cookie:
        print("  ! no cookie yet — game telemetry needs one (Cloudflare blocks without it)")

    # Warm the analytics token so the first /claim does not have to.
    if state.token and state.cookie:
        print("  … requesting analytics token from Discord")
        try:
            context = games_engine.RuntimeClientContext.initialize()
            games_engine.synchronize_credentials(state, context.encoded_properties)
        except Exception as exc:  # noqa: BLE001 - best effort
            print(f"  ! analytics token could not be fetched ({exc})")
            print("    claim may still work; retry /claim later")
        else:
            print("  ✓ analytics token cached")

    print("\n  saved to session.json — you can start the bot with: python -m butler run")
    return 0
