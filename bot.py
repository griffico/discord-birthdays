import datetime
import logging
import os
from zoneinfo import ZoneInfo

import aiohttp
from typing import Optional

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


class BirthdayBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        log.info("Command tree synced globally (may take up to 1 hour to propagate)")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s)", self.user, self.user.id)
        if not daily_check.is_running():
            await self._run_catchup()
            daily_check.start(self)

    async def _run_catchup(self) -> None:
        today_et = datetime.datetime.now(ET).date()
        for offset in range(CATCHUP_DAYS - 1, -1, -1):
            target = today_et - datetime.timedelta(days=offset)
            for guild in self.guilds:
                await announce(self, guild, target, today_et)
        for guild in self.guilds:
            await send_preview_dms(self, guild, today_et)


client = BirthdayBot()


# ── Opt-out view ──────────────────────────────────────────────────────────────

class OptOutView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: str, year: int) -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.year = year

    @discord.ui.button(label="Skip this year", style=discord.ButtonStyle.secondary)
    async def skip_year(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        storage.mark_skipped(self.guild_id, self.user_id, self.year)
        self.disable_all_buttons()
        await interaction.response.edit_message(
            content="Got it — your birthday announcement will be skipped this year. It'll resume next year automatically.",
            view=self,
        )

    @discord.ui.button(label="Remove me permanently", style=discord.ButtonStyle.danger)
    async def remove_permanently(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        storage.remove_birthday(self.guild_id, int(self.user_id))
        self.disable_all_buttons()
        await interaction.response.edit_message(
            content="You've been removed from birthday announcements. You can re-register anytime with `/birthday set`.",
            view=self,
        )

    def disable_all_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


# ── Slash commands ────────────────────────────────────────────────────────────

group = app_commands.Group(name="birthday", description="Birthday bot commands")


def parse_birthday(value: str) -> Optional[str]:
    value = value.strip()
    for fmt in ("%m-%d", "%m/%d", "%B %d", "%b %d", "%B %dst", "%B %dnd", "%B %drd", "%B %dth"):
        try:
            dt = datetime.datetime.strptime(value, fmt)
            return dt.strftime("%m-%d")
        except ValueError:
            continue
    return None


@group.command(name="set", description="Register your birthday (e.g. 03-14, 03/14, January 16)")
@app_commands.describe(date="Your birthday — try MM-DD, MM/DD, or 'January 16'")
async def birthday_set(interaction: discord.Interaction, date: str) -> None:
    mmdd = parse_birthday(date)
    if mmdd is None:
        await interaction.response.send_message(
            "Couldn't parse that date. Try `03-14`, `03/14`, or `March 14`.", ephemeral=True
        )
        return
    storage.set_birthday(interaction.guild_id, interaction.user.id, mmdd)
    await interaction.response.send_message(
        f"Your birthday has been set to **{mmdd}**. You'll be announced at noon ET on that day.",
        ephemeral=True,
    )


@group.command(name="remove", description="Remove your birthday and opt out of announcements")
async def birthday_remove(interaction: discord.Interaction) -> None:
    removed = storage.remove_birthday(interaction.guild_id, interaction.user.id)
    if removed:
        await interaction.response.send_message(
            "Your birthday has been removed.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "You don't have a birthday registered.", ephemeral=True
        )


@group.command(name="channel", description="Set the channel for birthday announcements")
@app_commands.describe(channel="The channel to post announcements in (defaults to current channel)")
async def birthday_channel(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
) -> None:
    target = channel or interaction.channel
    storage.set_channel(interaction.guild_id, target.id)
    await interaction.response.send_message(
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
async def birthday_status(
    interaction: discord.Interaction, days: int = 7
) -> None:
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
        await interaction.response.send_message(
            f"No birthdays in the last {days} day(s).", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"**Birthday status (last {days} day(s)):**\n" + "\n".join(lines),
            ephemeral=True,
        )


@group.command(name="admin-set", description="Set a birthday for another member")
@app_commands.describe(member="The member to set a birthday for", date="Their birthday — e.g. 03-14, 03/14, or 'March 14'")
async def birthday_admin_set(interaction: discord.Interaction, member: discord.Member, date: str) -> None:
    mmdd = parse_birthday(date)
    if mmdd is None:
        await interaction.response.send_message(
            "Couldn't parse that date. Try `03-14`, `03/14`, or `March 14`.", ephemeral=True
        )
        return
    storage.set_birthday(interaction.guild_id, member.id, mmdd)
    await interaction.response.send_message(
        f"Birthday for {member.mention} set to **{mmdd}**.", ephemeral=True
    )


@group.command(name="admin-clear", description="Reset all birthday data for this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def birthday_admin_clear(interaction: discord.Interaction) -> None:
    storage.clear_guild(interaction.guild_id)
    await interaction.response.send_message("All birthday data for this server has been cleared.", ephemeral=True)


@group.command(name="preview", description="Send preview DMs now to anyone with a birthday in the next 7 days")
async def birthday_preview(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    today_et = datetime.datetime.now(ET).date()
    await send_preview_dms(client, interaction.guild, today_et)
    await interaction.followup.send("Done — preview DMs sent to anyone with an upcoming birthday.", ephemeral=True)


@group.command(name="announce", description="Trigger today's birthday announcements now")
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

            member = guild.get_member(int(uid))
            if member is None:
                continue

            days_away = offset
            view = OptOutView(guild_id=guild.id, user_id=uid, year=year)
            try:
                await member.send(
                    f"👋 Hey! Just a heads up — your birthday ({mmdd}) is coming up in {days_away} day(s) "
                    f"and we'll be posting an announcement in **{guild.name}**. "
                    f"If you'd rather skip it this year or opt out entirely, use the buttons below.",
                    view=view,
                )
                storage.mark_preview_sent(guild.id, uid, year)
                log.info("Sent preview DM to user %s in guild %s (%s days away)", uid, guild.id, days_away)
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

        try:
            await channel.send(msg)
            storage.mark_wished(guild.id, target_date, uid)
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
