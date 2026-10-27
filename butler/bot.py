"""Owner-locked Discord bot that drives the local maximizer engines.

Run with:  python -m butler run

The bot never accepts tokens/cookies over Discord.  It only triggers work that
uses the local ``session.json``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import re
import sys
import traceback
from collections import deque
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .bridge import BadgeButler, ButlerError, ClaimResult, StreamSnapshot
from .config import Config

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

BRAND = 0x5865F2
OK = 0x57F287
WARN = 0xFEE75C
ERR = 0xED4245


# --------------------------------------------------------------------- stdout
class TeeStream(io.TextIOBase):
    """Mirror stdout into a ring buffer so ``/log`` can show engine output."""

    def __init__(self, original, max_lines: int = 400) -> None:
        self._original = original
        self._pending = ""
        self.lines: deque[str] = deque(maxlen=max_lines)

    def write(self, text: str) -> int:  # type: ignore[override]
        try:
            if self._original is not None:
                self._original.write(text)
                self._original.flush()
        except Exception:  # noqa: BLE001
            pass

        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            clean = ANSI_RE.sub("", line).rstrip()
            if clean.strip():
                self.lines.append(clean)
        return len(text)

    def flush(self) -> None:  # type: ignore[override]
        try:
            if self._original is not None:
                self._original.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:  # type: ignore[override]
        return False

    def reconfigure(self, **kwargs) -> None:  # type: ignore[override]
        """Pass encoding changes through to the real stdout.

        ``games.py`` and ``stream.py`` both call ``sys.stdout.reconfigure(...)``
        to force UTF-8; without this the Tee would swallow that call.
        """
        try:
            if self._original is not None and hasattr(self._original, "reconfigure"):
                self._original.reconfigure(**kwargs)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------- utils
def fmt_duration(seconds: float) -> str:
    total = int(max(0.0, seconds))
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def fmt_hours(value: float) -> str:
    return f"{value:,.0f}h"


def stream_embed(snapshot: StreamSnapshot) -> discord.Embed:
    if snapshot.running and snapshot.streaming:
        state = "🔴 LIVE — broadcasting"
        color = 0xEB459E
    elif snapshot.running:
        state = "⏳ connecting / negotiating"
        color = WARN
    else:
        state = "⚫ not running"
        color = 0x95A5A6

    embed = discord.Embed(title="Stream badge session", color=color)
    embed.add_field(name="State", value=state, inline=False)
    if snapshot.running:
        embed.add_field(name="Elapsed", value=fmt_duration(snapshot.elapsed_seconds), inline=True)
        embed.add_field(
            name="Voice", value="connected" if snapshot.voice_connected else "pending", inline=True
        )
        embed.add_field(
            name="Target",
            value=f"guild `{snapshot.guild_id}`\nchannel `{snapshot.channel_id}`",
            inline=False,
        )
    embed.add_field(name="Tracked total", value=fmt_hours(snapshot.total_hours), inline=True)
    embed.add_field(name="Sessions", value=str(snapshot.total_sessions), inline=True)
    embed.set_footer(text="hours sync to the badge 24–48h after Discord aggregates them")
    return embed


def claim_embed(result: ClaimResult) -> discord.Embed:
    ok = result.ok
    embed = discord.Embed(
        title="Game claim " + ("complete" if ok else "failed"),
        color=OK if ok else ERR,
    )
    embed.add_field(name="Titles accepted", value=f"{result.accepted} / {result.requested}", inline=True)
    embed.add_field(name="Hours per title", value=f"{result.hours_per_game:g}h", inline=True)
    embed.add_field(name="Hours credited", value=fmt_hours(result.total_hours), inline=True)
    if not ok:
        embed.description = (
            "Discord rejected every batch. Usual causes: expired token or "
            "expired `cf_clearance` cookie. Re-run `python -m butler auth`."
        )
    else:
        embed.description = (
            f"Catalog size: {result.catalog_size} detectable titles.\n"
            "Badge tiers update after Discord's analytics sync (24–48h)."
        )
    return embed


def auth_embed(butler: BadgeButler) -> discord.Embed:
    auth = butler.auth_status()
    game_ready = auth.has_token and auth.has_cookie
    embed = discord.Embed(
        title="Local credentials",
        color=OK if game_ready else WARN,
    )
    embed.add_field(name="Account token", value="✓ present" if auth.has_token else "✗ missing", inline=True)
    embed.add_field(name="cf_clearance", value="✓ present" if auth.has_cookie else "✗ missing", inline=True)
    embed.add_field(
        name="Analytics token",
        value=("✓ cached " + fmt_duration(auth.token_age_seconds) + " old") if auth.analytics_token else "— will be fetched",
        inline=True,
    )
    if auth.user_id:
        embed.add_field(name="User ID", value=f"`{auth.user_id}`", inline=True)
    embed.add_field(name="Games claimed", value=str(auth.games_claimed), inline=True)
    embed.add_field(name="Hours claimed", value=fmt_hours(auth.hours_claimed), inline=True)
    if auth.warnings:
        embed.add_field(name="Warnings", value="\n".join(f"• {w}" for w in auth.warnings), inline=False)
    return embed


def status_embed(bot: "ButlerBot") -> discord.Embed:
    auth = bot.butler.auth_status()
    stream = bot.butler.stream_status()

    embed = discord.Embed(title="Badge Maximizer · butler", color=BRAND)
    embed.add_field(
        name="Account",
        value=(
            f"token {'✓' if auth.has_token else '✗'} · "
            f"cookie {'✓' if auth.has_cookie else '✗'} · "
            f"game hours {fmt_hours(auth.hours_claimed)} · "
            f"stream {fmt_hours(auth.stream_hours)}"
        ),
        inline=False,
    )
    if stream.running:
        stream_value = (
            f"{'🔴 live' if stream.streaming else '⏳ connecting'} · "
            f"{fmt_duration(stream.elapsed_seconds)} · <#{stream.channel_id}>"
        )
    else:
        stream_value = "⚫ idle"
    embed.add_field(name="Stream", value=stream_value, inline=False)
    embed.add_field(name="Bot uptime", value=fmt_duration(bot.butler.uptime_seconds), inline=True)
    embed.add_field(name="Latency", value=f"{bot.latency * 1000:.0f} ms", inline=True)
    embed.set_footer(text="owner-only · credentials never leave this PC")
    return embed


# ------------------------------------------------------------------------ bot
class ButlerBot(commands.Bot):
    def __init__(self, config: Config, butler: BadgeButler, log_buffer: TeeStream) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(), help_command=None)
        self.config = config
        self.butler = butler
        self.log_buffer = log_buffer
        self.stream_announcement: Optional[discord.Message] = None
        self._announced = False

    async def setup_hook(self) -> None:
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[butler] {len(synced)} slash commands synced to guild {guild.id}")
        else:
            synced = await self.tree.sync()
            print(f"[butler] {len(synced)} slash commands synced globally (can take up to 1h)")
        self.watch_stream.start()

    async def on_ready(self) -> None:
        print(f"[butler] logged in as {self.user} ({self.user.id})")
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="badge progress")
        )
        if not self._announced:
            self._announced = True
            await self.notify_log(f"butler online as **{self.user}**")

    async def notify_log(self, text: str) -> Optional[discord.Message]:
        if not self.config.log_channel_id:
            return None
        channel = self.get_channel(self.config.log_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.log_channel_id)
            except Exception:  # noqa: BLE001
                return None
        try:
            return await channel.send(text)
        except Exception:  # noqa: BLE001
            return None

    @tasks.loop(minutes=5)
    async def watch_stream(self) -> None:
        snapshot = self.butler.stream_status()
        if self.butler.has_stream_engine and not snapshot.running:
            snapshot = await asyncio.to_thread(self.butler.reap_finished_stream)
        if self.stream_announcement is None:
            return
        try:
            await self.stream_announcement.edit(embed=stream_embed(snapshot))
        except Exception:  # noqa: BLE001
            pass
        if not snapshot.running:
            self.stream_announcement = None

    @watch_stream.before_loop
    async def _before_watch(self) -> None:
        await self.wait_until_ready()


# ------------------------------------------------------------------- commands
def register_commands(bot: ButlerBot, config: Config) -> None:
    default_games = min(max(int(config.default_games), 1), 500)
    default_hours = min(max(float(config.default_hours), 0.5), 2000.0)

    async def owner_only(interaction: discord.Interaction) -> bool:
        if interaction.user.id != config.owner_id:
            raise app_commands.CheckFailure("owner-only")
        return True

    tree = bot.tree

    @tree.error
    async def on_app_error(interaction: discord.Interaction, error: Exception) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "This bot is locked to its owner."
        else:
            traceback.print_exception(type(error), error, error.__traceback__)
            message = f"`{type(error).__name__}`: {error}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:  # noqa: BLE001
            pass

    @tree.command(name="status", description="Auth, badge totals and stream state")
    @app_commands.check(owner_only)
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=status_embed(bot), ephemeral=True)

    @tree.command(name="auth", description="Check stored credentials and analytics token")
    @app_commands.check(owner_only)
    async def auth(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=auth_embed(bot.butler), ephemeral=True)

    @tree.command(name="claim", description="Claim Game Time + Game Variety hours")
    @app_commands.describe(
        games="How many unique titles to claim (100 = Tier 10 variety)",
        hours="Hours credited per title (50 × 100 = 5,000h = Tier 10 time)",
    )
    @app_commands.check(owner_only)
    async def claim(
        interaction: discord.Interaction,
        games: app_commands.Range[int, 1, 500] = default_games,
        hours: app_commands.Range[float, 0.5, 2000.0] = default_hours,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await asyncio.to_thread(bot.butler.claim, int(games), float(hours))
        except ButlerError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            await interaction.followup.send(f"❌ unexpected error: {exc}", ephemeral=True)
            return

        await interaction.followup.send(embed=claim_embed(result), ephemeral=True)
        await bot.notify_log(
            f"claim: **{result.accepted}/{result.requested}** titles · "
            f"{result.hours_per_game:g}h each · {fmt_hours(result.total_hours)} credited"
        )

    # ------------------------------------------------------------- /stream
    stream_group = app_commands.Group(name="stream", description="Go Live stream session")

    @stream_group.command(name="start", description="Start the headless Go Live stream")
    @app_commands.describe(
        guild_id="Server ID (blank = value from butler/config.json)",
        channel_id="Voice channel ID (blank = value from config)",
    )
    @app_commands.check(owner_only)
    async def stream_start(
        interaction: discord.Interaction,
        guild_id: str = "",
        channel_id: str = "",
    ) -> None:
        config_guild, config_channel = config.voice_target()
        target_guild = (guild_id or config_guild).strip()
        target_channel = (channel_id or config_channel).strip()
        if not target_guild or not target_channel:
            await interaction.response.send_message(
                "Provide a server ID and voice channel ID, or set `voice_guild_id` / "
                "`voice_channel_id` in `butler/config.json` (Developer Mode → right-click → Copy ID).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            snapshot = await asyncio.to_thread(bot.butler.start_stream, target_guild, target_channel)
        except ButlerError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            await interaction.followup.send(f"❌ unexpected error: {exc}", ephemeral=True)
            return

        await interaction.followup.send(embed=stream_embed(snapshot), ephemeral=True)
        bot.stream_announcement = await bot.notify_log(
            f"🔴 **stream started** in <#{target_channel}> — reminder: keep **one viewer** "
            f"(alt/friend) in the channel and personalization **ON**."
        )
        if bot.stream_announcement is not None:
            try:
                await bot.stream_announcement.edit(embed=stream_embed(snapshot))
            except Exception:  # noqa: BLE001
                pass

    @stream_group.command(name="stop", description="Stop the stream and save the session")
    @app_commands.check(owner_only)
    async def stream_stop(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            snapshot = await asyncio.to_thread(bot.butler.stop_stream)
        except ButlerError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            await interaction.followup.send(f"❌ unexpected error: {exc}", ephemeral=True)
            return

        await interaction.followup.send(embed=stream_embed(snapshot), ephemeral=True)
        await bot.notify_log(
            f"⚫ stream stopped after **{fmt_duration(snapshot.elapsed_seconds)}** · "
            f"tracked total {fmt_hours(snapshot.total_hours)}"
        )
        bot.stream_announcement = None

    @stream_group.command(name="status", description="Show live stream state and elapsed time")
    @app_commands.check(owner_only)
    async def stream_state(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=stream_embed(bot.butler.stream_status()), ephemeral=True
        )

    tree.add_command(stream_group)

    # ------------------------------------------------------------- /profile
    @tree.command(name="profile", description="Inspect live profile badges")
    @app_commands.check(owner_only)
    async def profile(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await asyncio.to_thread(bot.butler.profile)
        except ButlerError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"❌ unexpected error: {exc}", ephemeral=True)
            return

        name = data["global_name"] or data["username"]
        embed = discord.Embed(title=f"{name} · profile badges", color=BRAND)
        embed.set_footer(text=f"id {data['id']}")
        if data["badges"]:
            embed.description = "\n".join(
                f"• `{badge['id']}` — {badge['description']}" for badge in data["badges"]
            )[:4000]
        else:
            embed.description = "No public badges on this profile."
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ----------------------------------------------------------------- /log
    @tree.command(name="log", description="Tail the local engine output")
    @app_commands.describe(lines="How many lines (1-80)")
    @app_commands.check(owner_only)
    async def log(interaction: discord.Interaction, lines: app_commands.Range[int, 1, 80] = 25) -> None:
        captured = list(bot.log_buffer.lines)[-int(lines) :]
        body = "\n".join(captured) if captured else "(no output captured yet)"
        if len(body) > 1900:
            body = body[-1900:]
        await interaction.response.send_message(f"```text\n{body}\n```", ephemeral=True)

    # -------------------------------------------------------------- /viewer
    viewer_group = app_commands.Group(
        name="viewer", description="Optional: keep a spectator in the voice channel"
    )

    @viewer_group.command(name="join", description="Make the bot sit in a voice channel as a viewer")
    @app_commands.check(owner_only)
    @app_commands.guild_only()
    async def viewer_join(interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
        if importlib.util.find_spec("nacl") is None:
            await interaction.response.send_message(
                "Voice requires PyNaCl: run `pip install PyNaCl` on the host PC, then retry.",
                ephemeral=True,
            )
            return
        if interaction.guild and interaction.guild.voice_client:
            await interaction.response.send_message("Bot is already in a voice channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await channel.connect(self_deaf=True, self_mute=True)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"❌ could not join voice: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ joined {channel.mention} as a viewer. A friend/alt is still the safer guarantee.",
            ephemeral=True,
        )

    @viewer_group.command(name="leave", description="Disconnect the bot from voice")
    @app_commands.check(owner_only)
    @app_commands.guild_only()
    async def viewer_leave(interaction: discord.Interaction) -> None:
        clients = list(bot.voice_clients)
        if not clients:
            await interaction.response.send_message("Bot is not in voice.", ephemeral=True)
            return
        for client in clients:
            await client.disconnect(force=True)
        await interaction.response.send_message("Left voice.", ephemeral=True)

    tree.add_command(viewer_group)

    @tree.command(name="ping", description="Check the bot is alive")
    @app_commands.check(owner_only)
    async def ping(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"pong · {bot.latency * 1000:.0f} ms · uptime {fmt_duration(bot.butler.uptime_seconds)}",
            ephemeral=True,
        )

    @tree.command(name="help", description="List available commands")
    @app_commands.check(owner_only)
    async def help_command(interaction: discord.Interaction) -> None:
        rows = []
        for command in bot.tree.get_commands():
            if isinstance(command, app_commands.Group):
                rows.append(f"**/{command.name}** — {command.description}")
                rows.extend(
                    f"  `/{command.name} {sub.name}` — {sub.description}"
                    for sub in command.commands
                )
            else:
                rows.append(f"**/{command.name}** — {command.description}")
        await interaction.response.send_message("\n".join(rows)[:1900], ephemeral=True)


# --------------------------------------------------------------------- wiring
def create_bot(config: Config) -> tuple[ButlerBot, TeeStream]:
    tee = TeeStream(sys.stdout)
    sys.stdout = tee  # type: ignore[assignment]

    butler = BadgeButler(config)
    bot = ButlerBot(config, butler, tee)

    def sink(message: str) -> None:
        try:
            loop = bot.loop
        except Exception:  # noqa: BLE001 - bot not running yet
            return
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(bot.notify_log(f"`{message}`"), loop)
        except Exception:  # noqa: BLE001
            pass

    butler._event_sink = sink  # noqa: SLF001 - intentional single wiring point

    register_commands(bot, config)
    return bot, tee
