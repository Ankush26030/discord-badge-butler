"""Butler configuration.

Values are read from ``butler/config.json`` and can be overridden with
environment variables (useful for keeping the bot token out of the file):

    BUTLER_BOT_TOKEN
    BUTLER_OWNER_ID
    BUTLER_GUILD_ID
    BUTLER_LOG_CHANNEL_ID
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
EXAMPLE_PATH = Path(__file__).resolve().parent / "config.example.json"


@dataclass
class Config:
    # --- required -------------------------------------------------------
    bot_token: str = ""
    owner_id: int = 0

    # Optional account credentials. If set, they are copied into session.json
    # on startup — handy on hosting panels where custom env vars are not
    # available. BUTLER_ACCOUNT_TOKEN / BUTLER_CF_CLEARANCE take precedence.
    account_token: str = ""
    cf_clearance: str = ""

    # --- optional -------------------------------------------------------
    # Sync slash commands to this guild for instant availability.
    # Leave 0 to sync globally (Discord can take up to an hour to show them).
    guild_id: int = 0
    # Channel where the bot posts stream start/stop + claim summaries.
    log_channel_id: int = 0

    # Defaults used by /claim when the arguments are omitted.
    default_games: int = 100
    default_hours: float = 50.0

    # Default voice target for /stream start (Channel ID, not guild name).
    voice_guild_id: str = ""
    voice_channel_id: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        data: dict = {}
        if path.exists():
            try:
                # utf-8-sig tolerates a BOM added by Windows Notepad/PowerShell.
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"[butler] {path} is not valid JSON: {exc}") from exc

        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in data.items() if k in known})

        cfg.bot_token = os.environ.get("BUTLER_BOT_TOKEN", "").strip() or cfg.bot_token
        cfg.owner_id = _as_int(os.environ.get("BUTLER_OWNER_ID")) or cfg.owner_id
        cfg.guild_id = _as_int(os.environ.get("BUTLER_GUILD_ID")) or cfg.guild_id
        cfg.log_channel_id = (
            _as_int(os.environ.get("BUTLER_LOG_CHANNEL_ID")) or cfg.log_channel_id
        )
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = path or CONFIG_PATH
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    def problems(self) -> list[str]:
        """Human readable list of things that must be fixed before running."""
        issues: list[str] = []
        if not self.bot_token or "." not in self.bot_token:
            issues.append(
                "bot_token is missing (set it in butler/config.json or BUTLER_BOT_TOKEN)."
            )
        if not self.owner_id:
            issues.append(
                "owner_id is missing (your numeric Discord user ID, not a username)."
            )
        if self.default_games < 1:
            issues.append("default_games must be >= 1.")
        if self.default_hours <= 0:
            issues.append("default_hours must be > 0.")
        return issues

    def voice_target(self) -> tuple[str, str]:
        return (str(self.voice_guild_id).strip(), str(self.voice_channel_id).strip())


def _as_int(value: str | None) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0
