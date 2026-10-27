"""``python -m butler`` entry point."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import CONFIG_PATH, EXAMPLE_PATH, Config


def run_check(config: Config) -> int:
    import discord

    import games as games_engine

    from .bot import create_bot

    print("butler self-check")
    print(f"  python           : {sys.version.split()[0]}")
    print(f"  discord.py       : {discord.__version__}")
    print(f"  repo root        : {CONFIG_PATH.parent.parent}")
    print(f"  config file      : {CONFIG_PATH} ({'found' if CONFIG_PATH.exists() else 'MISSING'})")

    problems = config.problems()
    if problems:
        print("  config           : incomplete")
        for problem in problems:
            print(f"    - {problem}")
    else:
        print("  config           : ok")

    auth = _auth_snapshot()
    print(
        "  credentials      : "
        f"token {'yes' if auth.has_token else 'no'} | "
        f"cookie {'yes' if auth.has_cookie else 'no'} | "
        f"user id {auth.user_id or '?'}"
    )

    try:
        catalog = games_engine.load_game_catalog()
        print(f"  game catalog     : {len(catalog)} detectable titles")
    except Exception as exc:  # noqa: BLE001
        print(f"  game catalog     : unavailable ({exc})")

    try:
        bot, _tee = create_bot(config)
        names = [command.name for command in bot.tree.get_commands()]
        print(f"  slash commands   : {', '.join(names)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  slash commands   : failed to build ({exc})")
        return 1

    if problems:
        print(f"\nnext step: copy {EXAMPLE_PATH.name} to config.json and fill it in")
        return 1
    print("\nready — start with: python -m butler run")
    return 0


def _auth_snapshot():
    from .bridge import BadgeButler

    return BadgeButler(Config()).auth_status()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m butler",
        description="Local Discord butler for the Badge Maximizer",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("run", help="start the bot (default)")
    subparsers.add_parser("auth", help="store account token + cf_clearance cookie")
    subparsers.add_parser("check", help="validate config and commands without connecting")
    parser.set_defaults(command="run")
    args = parser.parse_args(argv)

    if args.command == "auth":
        from .auth_cli import run as run_auth

        return run_auth()

    config = Config.load()

    if args.command == "check":
        seed_credentials(config)
        return run_check(config)

    seed_credentials(config)

    problems = config.problems()
    if problems:
        print("[butler] configuration incomplete:")
        for problem in problems:
            print(f"  - {problem}")
        print(f"\n  edit {CONFIG_PATH} (start from {EXAMPLE_PATH.name}) and run again.")
        return 2

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")

    import discord

    from .bot import create_bot

    bot, _tee = create_bot(config)
    _install_stop_signals()

    exit_code = 0
    try:
        bot.run(config.bot_token, log_handler=None)
    except discord.LoginFailure:
        print("[butler] Discord rejected the bot token. Check bot_token in butler/config.json.")
        exit_code = 2
    except KeyboardInterrupt:
        print("\n[butler] stop requested")
    except Exception as exc:  # noqa: BLE001
        print(f"[butler] failed to start: {type(exc).__name__}: {exc}")
        exit_code = 1
    finally:
        _shutdown_stream(bot)
    return exit_code


def seed_credentials(config: Config) -> None:
    """Copy account credentials into session.json without a TTY.

    Precedence: environment variables > ``butler/config.json`` > existing
    session.json.  Only namespaced variables are read, so a generic
    ``DISCORD_TOKEN`` meant for the *bot* is never mistaken for the account
    token:

        BUTLER_ACCOUNT_TOKEN   Discord account token
        BUTLER_CF_CLEARANCE    cf_clearance cookie value (or full cookie header)
    """
    import os

    import games as games_engine

    env_token = (os.environ.get("BUTLER_ACCOUNT_TOKEN") or "").strip()
    env_cookie = (os.environ.get("BUTLER_CF_CLEARANCE") or "").strip()
    token = env_token or (config.account_token or "").strip()
    cookie = games_engine.normalize_clearance_cookie(env_cookie or (config.cf_clearance or ""))
    if not token and not cookie:
        return

    state = games_engine.AccountSessionState.restore()
    changed = False
    if token and token != state.token.strip():
        state.token = token
        state.analytics_token = ""
        state.fetched_at = 0
        changed = True
    if cookie and cookie != state.cookie.strip():
        state.cookie = cookie
        changed = True

    if changed:
        state.persist()
        source = "environment variables" if env_token or env_cookie else "butler/config.json"
        print(f"[butler] account credentials loaded from {source}")


def _install_stop_signals() -> None:
    """Turn container stop signals into a clean shutdown."""
    import signal

    def _stop(signum, frame):  # noqa: ANN001, ARG001
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass


def _shutdown_stream(bot) -> None:  # noqa: ANN001
    butler = getattr(bot, "butler", None)
    if butler is None or not butler.has_stream_engine:
        return
    print("[butler] closing stream session…")
    try:
        snapshot = butler.stop_stream()
        print(f"[butler] stream session saved ({snapshot.elapsed_seconds / 3600:.2f}h)")
    except Exception as exc:  # noqa: BLE001
        print(f"[butler] stream shutdown failed: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
