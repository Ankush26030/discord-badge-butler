"""Thread-friendly wrapper around ``stream.GatewayStreamEngine``.

The upstream engine owns a blocking ``msvcrt`` keyboard loop inside
``start_stream()`` and ends with an ``input()`` prompt.  Neither is usable from
a Discord bot, so this subclass keeps the protocol logic and only replaces the
start/stop plumbing.
"""

from __future__ import annotations

import builtins
import threading
import time

import stream as stream_engine


class HeadlessStreamEngine(stream_engine.GatewayStreamEngine):
    """Same gateway/voice/Go-Live engine, minus the console keyboard loop."""

    def start_nonblocking(self, guild_id: str, channel_id: str) -> None:
        """Open the main gateway and start the event loop in a daemon thread.

        Mirrors ``GatewayStreamEngine.start_stream`` up to the point where it
        would block on the keyboard.  Raises if the gateway cannot be reached.
        """
        console = self.console
        console.info("connecting to discord gateway (wss://gateway.discord.gg)...")

        self.ws = stream_engine.MiniWebSocket(
            stream_engine.GATEWAY_URL, path="/?v=9&encoding=json"
        )
        self.ws.connect()

        self.running = True
        self.start_time = time.time()
        self.user_id = stream_engine._user_id_from_token(self.state.token) or ""
        self.stream_ever_active = False
        self._stream_recreate_attempts = 0
        self._user_left_vc = False

        threading.Thread(
            target=self._event_loop,
            args=(guild_id, channel_id),
            name="butler-gateway",
            daemon=True,
        ).start()

        self._status_thread = threading.Thread(
            target=self._status_display,
            name="butler-stream-status",
            daemon=True,
        )
        self._status_thread.start()

    def stop_blocking(self, guild_id: str, timeout: float = 8.0) -> float:
        """Stop the stream, leave voice and persist the session.

        Returns the elapsed seconds of the session.  ``_cleanup`` ends with an
        ``input()`` prompt, which is stubbed out for the duration of the call.
        """
        elapsed = max(0.0, time.time() - self.start_time) if self.start_time else 0.0
        self.running = False

        # Give the event loop a moment to notice ``running == False`` before the
        # sockets are torn down underneath it.
        deadline = time.time() + timeout
        while time.time() < deadline:
            loop_alive = any(
                t.name == "butler-gateway" and t.is_alive() for t in threading.enumerate()
            )
            if not loop_alive:
                break
            time.sleep(0.1)

        original_input = builtins.input
        builtins.input = lambda *args, **kwargs: ""  # type: ignore[assignment]
        try:
            self._cleanup(guild_id)
        finally:
            builtins.input = original_input  # type: ignore[assignment]

        return elapsed
