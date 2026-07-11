import json
import os
import tempfile
from datetime import date
from typing import Optional

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "birthdays.json")

_data: dict = {}


def load() -> None:
    global _data
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r") as f:
            _data = json.load(f)
    else:
        _data = {"guilds": {}}


def save() -> None:
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(DATA_PATH), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(_data, f, indent=2)
        os.replace(tmp_path, DATA_PATH)
    except Exception:
        os.unlink(tmp_path)
        raise


def _guild(guild_id: int) -> dict:
    gid = str(guild_id)
    if gid not in _data["guilds"]:
        _data["guilds"][gid] = {"channel_id": None, "birthdays": {}, "last_wished": {}}
    return _data["guilds"][gid]


def set_birthday(guild_id: int, user_id: int, mmdd: str) -> None:
    _guild(guild_id)["birthdays"][str(user_id)] = mmdd
    save()


def remove_birthday(guild_id: int, user_id: int) -> bool:
    g = _guild(guild_id)
    uid = str(user_id)
    if uid in g["birthdays"]:
        del g["birthdays"][uid]
        g.get("last_wished", {}).pop(uid, None)
        g.get("skip_year", {}).pop(uid, None)
        g.get("preview_sent", {}).pop(uid, None)
        save()
        return True
    return False


def set_channel(guild_id: int, channel_id: int) -> None:
    _guild(guild_id)["channel_id"] = channel_id
    save()


def get_channel(guild_id: int) -> Optional[int]:
    return _guild(guild_id).get("channel_id")


def all_birthdays(guild_id: int) -> dict:
    return dict(_guild(guild_id)["birthdays"])


def birthdays_on(guild_id: int, mmdd: str) -> list:
    return [uid for uid, bd in _guild(guild_id)["birthdays"].items() if bd == mmdd]


def was_wished(guild_id: int, target_date: date, user_id: str) -> bool:
    return _guild(guild_id).get("last_wished", {}).get(user_id) == target_date.year


def mark_wished(guild_id: int, target_date: date, user_id: str) -> None:
    g = _guild(guild_id)
    g.setdefault("last_wished", {})[user_id] = target_date.year
    save()


def last_wished_year(guild_id: int, user_id: str) -> Optional[int]:
    return _guild(guild_id).get("last_wished", {}).get(user_id)


# ── Skip this year ────────────────────────────────────────────────────────────

def was_skipped(guild_id: int, user_id: str, year: int) -> bool:
    return _guild(guild_id).get("skip_year", {}).get(user_id) == year


def mark_skipped(guild_id: int, user_id: str, year: int) -> None:
    _guild(guild_id).setdefault("skip_year", {})[user_id] = year
    save()


def clear_skip(guild_id: int, user_id: str) -> None:
    _guild(guild_id).get("skip_year", {}).pop(user_id, None)
    save()


def clear_wished(guild_id: int, user_id: str) -> None:
    _guild(guild_id).get("last_wished", {}).pop(user_id, None)
    save()


# ── Preview DM tracking ───────────────────────────────────────────────────────

def was_preview_sent(guild_id: int, user_id: str, year: int) -> bool:
    return _guild(guild_id).get("preview_sent", {}).get(user_id) == year


def mark_preview_sent(guild_id: int, user_id: str, year: int) -> None:
    _guild(guild_id).setdefault("preview_sent", {})[user_id] = year
    save()


def clear_guild(guild_id: int) -> None:
    gid = str(guild_id)
    if gid in _data["guilds"]:
        channel_id = _data["guilds"][gid].get("channel_id")
        _data["guilds"][gid] = {"channel_id": channel_id, "birthdays": {}, "last_wished": {}}
        save()


def all_guild_ids() -> list:
    return [int(gid) for gid in _data["guilds"]]
