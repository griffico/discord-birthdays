import datetime
import logging
import os
import re
from zoneinfo import ZoneInfo
from typing import Optional

import aiohttp
import discord
import discord.ext.tasks
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import storage

load_dotenv()
TOKEN = os.environ["DISCORD_TOKEN"]
CATCHUP_DAYS = int(os.getenv("CATCHUP_DAYS", "7"))
PREVIEW_DAYS = 7
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY")
ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Persistent opt-out buttons ────────────────────────────────────────────────
# custom_ids encode all context so buttons survive bot restarts.

class SkipButton(discord.ui.DynamicItem[discord.ui.Button], template=r"bday:skip:(?P<guild_id>\d+):(?P<year>\d+)"):
    def __init__(self, guild_id: int, year: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Skip this year",
                style=discord.ButtonStyle.secondary,
                custom_id=f"bday:skip:{guild_id}:{year}",
            )
        )
        self.guild_id = guild_id
        self.year = year

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match) -> "SkipButton":
        return cls(guild_id=int(match["guild_id"]), year=int(match["year"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        uid = str(interaction.user.id)
        storage.mark_skipped(self.guild_id, uid, self.year)
        await interaction.response.edit_message(
            content="Got it — your birthday announcement will be skipped this year. It'll resume next year automatically.\n"
                    "You can also use `/birthday skip` or `/birthday remove` any time.",
            view=None,
        )


class RemoveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"bday:remove:(?P<guild_id>\d+)"):
    def __init__(self, guild_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Remove me permanently",
                style=discord.ButtonStyle.danger,
                custom_id=f"bday:remove:{guild_id}",
            )
        )
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match) -> "RemoveButton":
        return cls(guild_id=int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        storage.remove_birthday(self.guild_id, interaction.user.id)
        await interaction.response.edit_message(
            content="You've been removed from birthday announcements. You can re-register anytime with `/birthday set` — only you'll see the response.",
            view=None,
        )


def make_opt_out_view(guild_id: int, year: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SkipButton(guild_id, year))
    view.add_item(RemoveButton(guild_id))
    return view


# ── Bot ───────────────────────────────────────────────────────────────────────

class BirthdayBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._catchup_done = False

    async def setup_hook(self) -> None:
        self.add_dynamic_items(SkipButton, RemoveButton)
        await self.tree.sync()
        log.info("Command tree synced globally (may take up to 1 hour to propagate)")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s)", self.user, self.user.id)
        if not self._catchup_done:
            self._catchup_done = True  # set before any await — prevents concurrent on_ready races
            await self._run_catchup()
        if not daily_check.is_running():
            daily_check.start(self)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = next(
                (
                    c for c in guild.text_channels
                    if c.permissions_for(guild.me).send_messages
                ),
                None,
            )
        if channel is None:
            log.warning("Joined guild %s but found no writable channel for welcome message", guild.id)
            return

        if not storage.get_channel(guild.id):
            storage.set_channel(guild.id, channel.id)
            channel_note = f"Birthday announcements will default to {channel.mention} — use `/birthday channel` to change it."
        else:
            channel_note = "*(Server admins: use `/birthday channel` to choose where announcements are posted.)*"

        await channel.send(
            "👋 **Thanks for adding Birthday Bot!**\n\n"
            "Members can register their birthday with `/birthday set <date>` — try `March 14` or `03-14`. "
            "Announcements go out automatically at noon ET.\n\n"
            "• `/birthday set <date>` — register your birthday\n"
            "• `/birthday remove` — opt out permanently\n"
            "• `/birthday skip` — skip just this year\n"
            "• `/birthday list` — see everyone's birthdays\n\n"
            "All commands are private — only you can see the response.\n\n"
            f"{channel_note}"
        )
        log.info("Sent welcome message to guild %s (channel %s)", guild.id, channel.id)

    async def _run_catchup(self) -> None:
        today_et = datetime.datetime.now(ET).date()
        for offset in range(CATCHUP_DAYS - 1, -1, -1):
            target = today_et - datetime.timedelta(days=offset)
            for guild in self.guilds:
                await announce(self, guild, target, today_et)
        for guild in self.guilds:
            await send_preview_dms(self, guild, today_et)


client = BirthdayBot()


# ── Global error handler ──────────────────────────────────────────────────────

async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        msg = "You don't have permission to use this command."
    else:
        log.error("Unhandled command error: %s", error, exc_info=error)
        msg = "Something went wrong. Please try again."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


client.tree.on_error = on_app_command_error


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_birthday(value: str) -> Optional[str]:
    value = value.strip()
    for fmt in ("%m-%d", "%m/%d", "%B %d", "%b %d", "%B %dst", "%B %dnd", "%B %drd", "%B %dth"):
        try:
            dt = datetime.datetime.strptime(value, fmt)
            return dt.strftime("%m-%d")
        except ValueError:
            continue
    return None


def next_birthday_year(mmdd: str, today: datetime.date) -> int:
    """Return the year of the next upcoming announcement for a given MM-DD."""
    try:
        bd = datetime.datetime.strptime(f"{today.year}-{mmdd}", "%Y-%m-%d").date()
    except ValueError:
        return today.year
    return today.year if bd >= today else today.year + 1


# ── Slash commands ────────────────────────────────────────────────────────────

group = app_commands.Group(name="birthday", description="Birthday bot commands")


@group.command(name="set", description="Register your birthday (e.g. 03-14, 03/14, January 16)")
@app_commands.describe(date="Your birthday — try MM-DD, MM/DD, or 'January 16'")
async def birthday_set(interaction: discord.Interaction, date: str) -> None:
    await interaction.response.defer(ephemeral=True)
    mmdd = parse_birthday(date)
    if mmdd is None:
        await interaction.followup.send(
            "Couldn't parse that date. Try `03-14`, `03/14`, or `March 14`.", ephemeral=True
        )
        return
    storage.set_birthday(interaction.guild_id, interaction.user.id, mmdd)
    storage.clear_wished(interaction.guild_id, str(interaction.user.id))
    await interaction.followup.send(
        f"Your birthday has been set to **{mmdd}**. You'll be announced at noon ET on that day.",
        ephemeral=True,
    )


@group.command(name="remove", description="Remove your birthday and opt out of announcements")
async def birthday_remove(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    removed = storage.remove_birthday(interaction.guild_id, interaction.user.id)
    if removed:
        await interaction.followup.send("Your birthday has been removed.", ephemeral=True)
    else:
        await interaction.followup.send("You don't have a birthday registered.", ephemeral=True)


@group.command(name="skip", description="Skip this year's birthday announcement")
async def birthday_skip(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    uid = str(interaction.user.id)
    birthdays = storage.all_birthdays(interaction.guild_id)
    if uid not in birthdays:
        await interaction.followup.send("You don't have a birthday registered.", ephemeral=True)
        return
    today_et = datetime.datetime.now(ET).date()
    year = next_birthday_year(birthdays[uid], today_et)
    storage.mark_skipped(interaction.guild_id, uid, year)
    await interaction.followup.send(
        f"Got it — your birthday announcement will be skipped for {year}. It'll resume next year automatically.",
        ephemeral=True,
    )


@group.command(name="help", description="How to use the birthday bot")
async def birthday_help(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(
        "**Birthday Bot — Quick Guide**\n"
        "All commands are private — only you can see the responses, and no one else sees you running them.\n\n"
        "**Getting started**\n"
        "`/birthday set <date>` — Register your birthday. Accepts `03-14`, `03/14`, or `March 14`.\n"
        "`/birthday remove` — Remove yourself from announcements permanently.\n"
        "`/birthday skip` — Skip just this year's announcement. You'll be included again next year.\n\n"
        "**Other commands**\n"
        "`/birthday list` — See everyone's registered birthdays.\n"
        "`/birthday status` — See recent birthdays and whether they were announced.\n\n"
        "**Privacy**\n"
        "A week before your birthday, the bot will send you a private DM with the option to skip or remove yourself.",
        ephemeral=True,
    )


@group.command(name="channel", description="Set the channel for birthday announcements")
@app_commands.describe(channel="The channel to post announcements in (defaults to current channel)")
async def birthday_channel(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    target = channel or interaction.channel
    storage.set_channel(interaction.guild_id, target.id)
    await interaction.followup.send(
        f"Birthday announcements will be posted in {target.mention}.", ephemeral=True
    )


@group.command(name="list", description="List all registered birthdays in this server")
async def birthday_list(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    birthdays = storage.all_birthdays(interaction.guild_id)
    if not birthdays:
        await interaction.followup.send(
            "No birthdays registered yet. Use `/birthday set` to add yours!", ephemeral=True
        )
        return

    lines = []
    for uid, mmdd in sorted(birthdays.items(), key=lambda x: x[1]):
        lines.append(f"<@{uid}> — {mmdd}")

    await interaction.followup.send(
        "**Registered birthdays:**\n" + "\n".join(lines), ephemeral=True
    )


@group.command(name="status", description="Show recent birthdays and whether notices were sent")
@app_commands.describe(days="How many days back to check (default 7)")
async def birthday_status(interaction: discord.Interaction, days: int = 7) -> None:
    await interaction.response.defer(ephemeral=True)
    days = max(1, min(days, 30))
    today_et = datetime.datetime.now(ET).date()
    lines = []

    for offset in range(days - 1, -1, -1):
        target = today_et - datetime.timedelta(days=offset)
        mmdd = target.strftime("%m-%d")
        members = storage.birthdays_on(interaction.guild_id, mmdd)

        if not members and target.month == 2 and target.day == 28:
            members = members + storage.birthdays_on(interaction.guild_id, "02-29")

        if not members:
            continue

        for uid in members:
            wished = storage.was_wished(interaction.guild_id, target, uid)
            status_icon = "✅" if wished else "❌"
            label = "today" if offset == 0 else target.isoformat()
            lines.append(f"{status_icon} <@{uid}> — {mmdd} ({label})")

    if not lines:
        await interaction.followup.send(
            f"No birthdays in the last {days} day(s).", ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"**Birthday status (last {days} day(s)):**\n" + "\n".join(lines),
            ephemeral=True,
        )


@group.command(name="admin-set", description="Set a birthday for another member")
@app_commands.describe(member="The member to set a birthday for", date="Their birthday — e.g. 03-14, 03/14, or 'March 14'")
@app_commands.checks.has_permissions(manage_guild=True)
async def birthday_admin_set(interaction: discord.Interaction, member: discord.Member, date: str) -> None:
    await interaction.response.defer(ephemeral=True)
    mmdd = parse_birthday(date)
    if mmdd is None:
        await interaction.followup.send(
            "Couldn't parse that date. Try `03-14`, `03/14`, or `March 14`.", ephemeral=True
        )
        return
    storage.set_birthday(interaction.guild_id, member.id, mmdd)
    storage.clear_wished(interaction.guild_id, str(member.id))
    await interaction.followup.send(
        f"Birthday for {member.mention} set to **{mmdd}**.", ephemeral=True
    )


@group.command(name="admin-clear", description="Reset all birthday data for this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def birthday_admin_clear(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    storage.clear_guild(interaction.guild_id)
    await interaction.followup.send("All birthday data for this server has been cleared.", ephemeral=True)


@group.command(name="preview", description="Send preview DMs now to anyone with a birthday in the next 7 days")
@app_commands.checks.has_permissions(manage_guild=True)
async def birthday_preview(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    today_et = datetime.datetime.now(ET).date()
    await send_preview_dms(client, interaction.guild, today_et)
    await interaction.followup.send("Done — preview DMs sent to anyone with an upcoming birthday.", ephemeral=True)


@group.command(name="announce", description="Trigger today's birthday announcements now")
@app_commands.checks.has_permissions(manage_guild=True)
async def birthday_announce(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    today_et = datetime.datetime.now(ET).date()
    await announce(client, interaction.guild, today_et, today_et)
    await interaction.followup.send("Done — any unannounced birthdays today have been posted.", ephemeral=True)


client.tree.add_command(group)


# ── Daily scheduler ───────────────────────────────────────────────────────────

@tasks.loop(time=datetime.time(hour=12, tzinfo=ET))
async def daily_check(bot: discord.Client) -> None:
    today_et = datetime.datetime.now(ET).date()
    log.info("Daily check firing for %s", today_et)
    for guild in bot.guilds:
        await announce(bot, guild, today_et, today_et)
        await send_preview_dms(bot, guild, today_et)


# ── Preview DMs ───────────────────────────────────────────────────────────────

async def send_preview_dms(bot: discord.Client, guild: discord.Guild, today: datetime.date) -> None:
    for offset in range(1, PREVIEW_DAYS + 1):
        preview_date = today + datetime.timedelta(days=offset)
        mmdd = preview_date.strftime("%m-%d")
        members = storage.birthdays_on(guild.id, mmdd)

        for uid in members:
            year = preview_date.year
            if storage.was_preview_sent(guild.id, uid, year):
                continue
            if storage.was_skipped(guild.id, uid, year):
                continue

            view = make_opt_out_view(guild_id=guild.id, year=year)
            try:
                user = await bot.fetch_user(int(uid))
                await user.send(
                    f"👋 Hey! Just a heads up — your birthday ({mmdd}) is coming up in {offset} day(s) "
                    f"and we'll be posting an announcement in **{guild.name}**. "
                    f"If you'd rather skip it this year or opt out entirely, use the buttons below.",
                    view=view,
                )
                storage.mark_preview_sent(guild.id, uid, year)
                log.info("Sent preview DM to user %s in guild %s (%s days away)", uid, guild.id, offset)
            except discord.NotFound:
                log.warning("User %s not found, skipping preview DM", uid)
            except discord.Forbidden:
                log.warning("Could not DM user %s (DMs disabled)", uid)
            except discord.HTTPException as e:
                log.error("Failed to send preview DM to user %s: %s", uid, e)


# ── Giphy ─────────────────────────────────────────────────────────────────────

async def fetch_birthday_gif() -> Optional[str]:
    if not GIPHY_API_KEY:
        return None
    url = "https://api.giphy.com/v1/gifs/random"
    params = {"api_key": GIPHY_API_KEY, "tag": "funny happy birthday", "rating": "pg"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                log.info("Giphy response status: %s", resp.status)
                if resp.status == 200:
                    data = await resp.json()
                    gif_url = data["data"]["images"]["original"]["url"]
                    log.info("Giphy GIF fetched: %s", gif_url)
                    return gif_url
                else:
                    body = await resp.text()
                    log.warning("Giphy error %s: %s", resp.status, body)
    except Exception as e:
        log.warning("Failed to fetch Giphy GIF: %s", e)
    return None


# ── Announcement core ─────────────────────────────────────────────────────────

async def announce(
    bot: discord.Client,
    guild: discord.Guild,
    target_date: datetime.date,
    today: datetime.date,
) -> None:
    channel_id = storage.get_channel(guild.id)
    if not channel_id:
        return

    mmdd = target_date.strftime("%m-%d")
    members = storage.birthdays_on(guild.id, mmdd)

    # Handle Feb 29 in non-leap years: announce on Feb 28
    if not members and target_date.month == 2 and target_date.day == 28:
        members = storage.birthdays_on(guild.id, "02-29")

    if not members:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        log.warning("Channel %s not found in guild %s", channel_id, guild.id)
        return

    is_belated = target_date < today

    for uid in members:
        if storage.was_wished(guild.id, target_date, uid):
            continue
        if storage.was_skipped(guild.id, uid, target_date.year):
            log.info("Skipping announcement for user %s (opted out this year)", uid)
            continue

        mention = f"<@{uid}>"
        if is_belated:
            msg = f"🎂 Belated happy birthday, {mention}! (Their birthday was {target_date.isoformat()})"
        else:
            msg = f"🎉 Happy birthday, {mention}!"

        gif_url = await fetch_birthday_gif()
        if gif_url:
            msg = f"{msg}\n{gif_url}"

        storage.mark_wished(guild.id, target_date, uid)
        try:
            await channel.send(msg)
            log.info(
                "Announced %s birthday for user %s in guild %s (channel %s)",
                "belated" if is_belated else "on-day",
                uid,
                guild.id,
                channel_id,
            )
        except discord.Forbidden:
            log.error("No permission to post in channel %s (guild %s)", channel_id, guild.id)
        except discord.HTTPException as e:
            log.error("Failed to post birthday message: %s", e)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    storage.load()
    client.run(TOKEN)
