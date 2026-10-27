"""Glue between the Discord bot and the existing maximizer engines.

Nothing here re-implements protocol details: game claiming is delegated to
``games.IngestionPipeline`` and streaming to ``stream.GatewayStreamEngine``
(via :class:`butler.engine.HeadlessStreamEngine`).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

import games as games_engine
import stream as stream_engine

from .config import Config
from .engine import HeadlessStreamEngine

EventSink = Callable[[str], None]


class ButlerError(RuntimeError):
    """Raised for anything the user can fix: missing auth, bad IDs, etc."""


@dataclass
class ClaimResult:
    requested: int
    accepted: int
    hours_per_game: float
    total_hours: float
    catalog_size: int

    @property
    def ok(self) -> bool:
        return self.accepted > 0


@dataclass
class StreamSnapshot:
    running: bool = False
    streaming: bool = False
    voice_connected: bool = False
    elapsed_seconds: float = 0.0
    guild_id: str = ""
    channel_id: str = ""
    total_hours: float = 0.0
    total_sessions: int = 0


@dataclass
class AuthSnapshot:
    has_token: bool = False
    has_cookie: bool = False
    user_id: str = ""
    analytics_token: bool = False
    token_age_seconds: float = 0.0
    games_claimed: int = 0
    hours_claimed: float = 0.0
    stream_hours: float = 0.0
    stream_sessions: int = 0
    warnings: list[str] = field(default_factory=list)


def verify_user_token(token: str) -> dict:
    """GET /users/@me with the account token.  Raises ButlerError on failure."""
    request = urllib.request.Request(
        "https://discord.com/api/v9/users/@me",
        headers={
            "authorization": token.strip(),
            "user-agent": games_engine.USER_AGENT_IDENTIFIER,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ButlerError("Discord rejected the token (HTTP 401). Paste a fresh one.") from exc
        raise ButlerError(f"Discord returned HTTP {exc.code} while checking the token.") from exc
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        raise ButlerError(f"Could not reach Discord: {exc}") from exc


class BadgeButler:
    """Owner-scoped façade used by the bot."""

    def __init__(self, config: Config, event_sink: Optional[EventSink] = None) -> None:
        self.config = config
        self._event_sink = event_sink
        self._claim_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._engine: Optional[HeadlessStreamEngine] = None
        self._stream_guild = ""
        self._stream_channel = ""
        self._stream_started = 0.0
        self._started_at = time.time()

    # ------------------------------------------------------------------ misc
    def _emit(self, message: str) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(message)
        except Exception:  # noqa: BLE001 - never let logging break work
            pass

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._started_at

    # ------------------------------------------------------------------ auth
    def auth_status(self) -> AuthSnapshot:
        game_state = games_engine.AccountSessionState.restore()
        stream_state = stream_engine.State.load()

        snapshot = AuthSnapshot(
            has_token=bool(game_state.token.strip()),
            has_cookie=bool(game_state.cookie.strip()),
            user_id=games_engine.extract_account_snowflake(game_state.token)
            if game_state.token
            else "",
            analytics_token=bool(game_state.analytics_token),
            token_age_seconds=max(0.0, time.time() - game_state.fetched_at)
            if game_state.fetched_at
            else 0.0,
            games_claimed=game_state.total_games_claimed,
            hours_claimed=game_state.total_hours_claimed,
            stream_hours=stream_state.total_stream_hours_claimed,
            stream_sessions=stream_state.total_stream_sessions,
        )

        if not snapshot.has_token:
            snapshot.warnings.append(
                "No account token. Run `python -m butler auth` on the PC."
            )
        if not snapshot.has_cookie:
            snapshot.warnings.append(
                "No cf_clearance cookie. Run `python -m butler auth` on the PC."
            )
        if snapshot.has_token and snapshot.token_age_seconds > 12 * 3600:
            snapshot.warnings.append(
                "Analytics token is older than 12h and will be refreshed on the next run."
            )
        return snapshot

    def _require_account(self) -> games_engine.AccountSessionState:
        state = games_engine.AccountSessionState.restore()
        if not state.token.strip():
            raise ButlerError(
                "No Discord account token configured. Run `python -m butler auth` in the project folder."
            )

        normalized = games_engine.normalize_clearance_cookie(state.cookie)
        if normalized != state.cookie:
            state.cookie = normalized
            state.persist()

        if not normalized:
            self._emit(
                "claim: no cf_clearance cookie saved — if Discord answers 403, add one to config.json"
            )
        return state

    # ----------------------------------------------------------------- claim
    def claim(self, games_count: int, hours_per_game: float) -> ClaimResult:
        if not self._claim_lock.acquire(blocking=False):
            raise ButlerError("A game claim is already running.")
        try:
            state = self._require_account()

            count = max(1, int(games_count))
            hours = float(hours_per_game)
            if hours <= 0:
                raise ButlerError("hours must be greater than 0.")

            self._emit(f"claim: loading detectable game catalog…")
            catalog = games_engine.load_game_catalog()
            if not catalog:
                raise ButlerError(
                    "Could not load Discord's detectable games catalog (network problem?)."
                )

            selected = self._select_titles(state, catalog, count)
            if not selected:
                raise ButlerError("No titles selected for this run.")

            context = games_engine.RuntimeClientContext.initialize()
            fabricator = games_engine.TelemetryEventFabricator(state, context)
            terminal = games_engine.TerminalInterface()

            self._emit(f"claim: requesting analytics credentials…")
            try:
                games_engine.synchronize_credentials(state, context.encoded_properties)
            except RuntimeError as exc:
                raise ButlerError(str(exc)) from exc

            pipeline = games_engine.IngestionPipeline(fabricator, terminal)
            duration_ms = int(hours * 3600 * 1000)

            self._emit(
                f"claim: submitting {hours:g}h across {len(selected)} titles "
                f"({len(selected) * hours:,.0f}h total)…"
            )
            accepted = pipeline.execute_stream(
                selected,
                lambda title: fabricator.assemble_complete_session(title, duration_ms),
            )

            if accepted:
                state.total_games_claimed += accepted
                state.total_hours_claimed += accepted * hours
                state.persist()

            return ClaimResult(
                requested=len(selected),
                accepted=accepted,
                hours_per_game=hours,
                total_hours=accepted * hours,
                catalog_size=len(catalog),
            )
        finally:
            self._claim_lock.release()

    @staticmethod
    def _select_titles(
        state: games_engine.AccountSessionState,
        catalog: list[games_engine.DetectableTitle],
        count: int,
    ) -> list[games_engine.DetectableTitle]:
        """Rotate through the catalog the same way ``games.py`` option 1 does."""
        existing = {item.title_id for item in catalog}
        unprocessed = [t.title_id for t in catalog if t.title_id not in state.used_games]
        processed = [game_id for game_id in state.used_games if game_id in existing]

        chosen = unprocessed[:count]
        if len(chosen) < count and processed:
            needed = count - len(chosen)
            rotated = processed[needed:] + processed[:needed]
            chosen.extend(rotated[:needed])

        state.used_games = [g for g in state.used_games if g not in chosen] + chosen
        registry = {item.title_id: item for item in catalog}
        return [registry[game_id] for game_id in chosen if game_id in registry]

    # ---------------------------------------------------------------- stream
    @property
    def has_stream_engine(self) -> bool:
        return self._engine is not None

    def reap_finished_stream(self) -> StreamSnapshot:
        """Clean up an engine that died on its own (server ended the stream)."""
        with self._stream_lock:
            self._retire_dead_engine()
            return self.stream_status()

    def stream_status(self) -> StreamSnapshot:
        stream_state = stream_engine.State.load()
        engine = self._engine
        running = bool(engine and engine.running)
        snapshot = StreamSnapshot(
            running=running,
            streaming=bool(engine and engine.stream_connected),
            voice_connected=bool(engine and engine.voice_connected),
            elapsed_seconds=max(0.0, time.time() - self._stream_started)
            if running and self._stream_started
            else 0.0,
            guild_id=self._stream_guild,
            channel_id=self._stream_channel,
            total_hours=stream_state.total_stream_hours_claimed,
            total_sessions=stream_state.total_stream_sessions,
        )
        return snapshot

    def start_stream(self, guild_id: str, channel_id: str) -> StreamSnapshot:
        with self._stream_lock:
            self._retire_dead_engine()
            if self._engine and self._engine.running:
                raise ButlerError("A stream is already running. Use /stream stop first.")

            guild_id = str(guild_id).strip()
            channel_id = str(channel_id).strip()
            if not guild_id.isdigit() or not channel_id.isdigit():
                raise ButlerError(
                    "Server ID and voice channel ID must be numeric snowflakes "
                    "(enable Developer Mode -> right-click -> Copy ID)."
                )

            state = stream_engine.State.load()
            if not state.token.strip():
                raise ButlerError(
                    "No Discord account token configured. Run `python -m butler auth` first."
                )

            console = stream_engine.Console()
            session = stream_engine.Session.new()

            self._emit("stream: validating account token…")
            try:
                stream_engine.ensure_token(state, session.super_props)
            except Exception as exc:  # noqa: BLE001 - message is user facing
                raise ButlerError(f"Discord rejected the account token: {exc}") from exc

            self._emit("stream: validating server, channel and permissions…")
            valid, reason = stream_engine.validate_stream_target(
                token=state.token,
                super_props=session.super_props,
                guild_id=guild_id,
                channel_id=channel_id,
                console=console,
            )
            if not valid:
                raise ButlerError(f"Stream target rejected: {reason}")

            engine = HeadlessStreamEngine(state, session, console)
            self._emit("stream: opening gateway and joining voice…")
            try:
                engine.start_nonblocking(guild_id, channel_id)
            except Exception as exc:  # noqa: BLE001
                raise ButlerError(f"Could not connect to the Discord gateway: {exc}") from exc

            state.last_guild_id = guild_id
            state.last_channel_id = channel_id
            state.save()

            self._engine = engine
            self._stream_guild = guild_id
            self._stream_channel = channel_id
            self._stream_started = time.time()

            return self.stream_status()

    def stop_stream(self) -> StreamSnapshot:
        with self._stream_lock:
            engine = self._engine
            if engine is None:
                raise ButlerError("No stream is running.")

            guild_id = self._stream_guild
            elapsed = engine.stop_blocking(guild_id)
            self._engine = None
            self._stream_guild = ""
            self._stream_channel = ""
            self._stream_started = 0.0

            snapshot = self.stream_status()
            snapshot.elapsed_seconds = elapsed
            return snapshot

    def _retire_dead_engine(self) -> None:
        """Clean up an engine that stopped itself (server kicked the stream)."""
        engine = self._engine
        if engine is None or engine.running:
            return
        guild_id = self._stream_guild
        try:
            engine.stop_blocking(guild_id)
        except Exception:  # noqa: BLE001
            pass
        self._engine = None
        self._stream_guild = ""
        self._stream_channel = ""
        self._stream_started = 0.0

    # --------------------------------------------------------------- profile
    def profile(self) -> dict:
        state = self._require_account()
        user_id = games_engine.extract_account_snowflake(state.token)
        if not user_id:
            raise ButlerError("Token does not look like a Discord account token.")

        context = games_engine.RuntimeClientContext.initialize()
        url = (
            f"{games_engine.DISCORD_USERS_API}/{user_id}/profile"
            "?with_mutual_guilds=false&with_mutual_friends_count=false"
        )
        request = urllib.request.Request(
            url,
            headers={
                "authorization": state.token,
                "user-agent": games_engine.USER_AGENT_IDENTIFIER,
                "x-super-properties": context.encoded_properties,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ButlerError(f"Profile request failed with HTTP {exc.code}.") from exc
        except Exception as exc:  # noqa: BLE001
            raise ButlerError(f"Profile request failed: {exc}") from exc

        user = payload.get("user", {}) or {}
        badges = payload.get("badges", []) or []
        return {
            "id": user.get("id", user_id),
            "username": user.get("username", "unknown"),
            "global_name": user.get("global_name") or "",
            "badges": [
                {
                    "id": str(badge.get("id", "badge")),
                    "description": badge.get("description", "") or badge.get("id", ""),
                }
                for badge in badges
            ],
        }
