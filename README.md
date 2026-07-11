# Discord Birthday Bot

A Discord bot that announces team birthdays at 8:00 AM Eastern Time. Members opt in by registering their birthday via a slash command. If the bot was offline on someone's birthday, it automatically posts a belated notice when it comes back up.

## Commands

| Command | Who can use | Description |
|---|---|---|
| `/birthday set <date>` | Anyone | Register your birthday (e.g. `03-14`, `03/14`, `March 14`). Opts you in. |
| `/birthday remove` | Anyone | Remove your birthday and opt out permanently. |
| `/birthday skip` | Anyone | Skip this year's announcement only. Resumes next year automatically. |
| `/birthday channel [#channel]` | Anyone | Set where announcements are posted (defaults to current channel). |
| `/birthday list` | Anyone | Show all registered birthdays for this server. |
| `/birthday status [days]` | Anyone | Show recent birthdays and whether each notice was sent (✅/❌). |
| `/birthday admin-set @member <date>` | Manage Server | Set a birthday on behalf of another member. |
| `/birthday admin-clear` | Manage Server | Reset all birthday data for this server. |
| `/birthday announce` | Manage Server | Trigger today's birthday announcements immediately. |
| `/birthday preview` | Manage Server | Send preview DMs now to anyone with a birthday in the next 7 days. |

## Setup

### Prerequisites
- Python 3.8 or newer (`python3 --version`)
- A Discord bot application with a valid token ([Discord Developer Portal](https://discord.com/developers/applications))
- Bot invited to your server with `bot` and `applications.commands` scopes and **Send Messages** permission enabled (OAuth2 tab → Bot → check Send Messages)

### Install

```bash
# Clone or copy the repo
git clone <repo-url> discord-birthday
cd discord-birthday

# Create a virtual environment (keeps dependencies isolated)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Configure your token
cp .env.example .env
# Edit .env and set DISCORD_TOKEN=your-token-here
```

### Run locally

```bash
.venv/bin/python bot.py
```

Slash commands are synced globally on startup (may take up to an hour to appear everywhere).

## Raspberry Pi B Deployment

The original Raspberry Pi Model B (ARMv6, 32-bit) is fully supported. All dependencies are pure-Python with no native compilation step.

### Copy files to the Pi

```bash
# From your development machine
rsync -av --exclude='.venv' --exclude='data' --exclude='.env' \
  discord-birthday/ pi@<pi-ip>:/home/pi/discord-birthday/
```

### Set up on the Pi

```bash
ssh pi@<pi-ip>

cd /home/pi/discord-birthday
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env  # add your DISCORD_TOKEN
```

### Install as a systemd service

```bash
sudo cp deploy/discord-birthday.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable discord-birthday
sudo systemctl start discord-birthday

# Check logs
journalctl -u discord-birthday -f
```

The bot will now start automatically on boot and restart if it crashes.

## How it works

- **12:00 PM ET daily:** the bot checks today's date and posts birthday messages for any registered members. DST is handled automatically.
- **On startup:** the bot runs a catch-up pass. If it was offline when someone's birthday occurred (within the last 7 days), it posts a belated notice. Already-sent notices are never re-sent.
- **Idempotent:** every announcement is recorded in `data/birthdays.json`. Restarting the bot never causes duplicate posts.
- **Feb 29 birthdays** are announced on Feb 28 in non-leap years.

### Configuration

Set in `.env`:

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | (required) | Your Discord bot token |
| `CATCHUP_DAYS` | `7` | How many days back to auto-post belated notices on startup |

## Permissions note

Currently any server member can use all commands, including `/birthday channel`. To restrict channel configuration to users with Manage Server permission, add `@app_commands.checks.has_permissions(manage_guild=True)` to the `birthday_channel` command in `bot.py`.

## Data

Birthday data is stored in `data/birthdays.json` (git-ignored). Back this file up if you want to preserve registrations across reinstalls.
