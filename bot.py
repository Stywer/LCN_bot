import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

MAX_DISCORD_MESSAGE_LEN = 1900
try:
    MSK_TZ = ZoneInfo("Europe/Moscow")
except Exception:
    MSK_TZ = timezone(timedelta(hours=3), name="MSK")
REMINDERS_FILE = "reminders.json"
REMINDER_VOTES_FILE = "reminder_votes.json"
REMINDER_DISPATCHES_FILE = "reminder_dispatches.json"
REMINDER_DM_LINKS_FILE = "reminder_dm_links.json"


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip()
    if normalized == "":
        return default

    return int(normalized)


def truncate_cell(value: str, max_len: int = 24) -> str:
    normalized = value.replace("\n", " ").replace("\r", " ").strip()
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3] + "..."


def truncate_text(value: str, max_len: int) -> str:
    text = value.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def format_reminder_description(value: str) -> str:
    text = value.strip()
    if not text:
        return ""

    # Buttons already handle choice, so remove legacy instruction text if present.
    text = re.sub(
        r"выберите:\s*✅\s*-\s*буду\s*❌\s*-\s*не\s*смогу",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s{2,}", " ", text).strip()

    # Make very long one-line descriptions easier to read.
    if "\n" not in text and len(text) > 220:
        text = re.sub(r"\.\s+", ".\n", text)

    return text.strip()


def split_title_mentions(title_text: str) -> tuple[str, list[str]]:
    mentions = re.findall(r"<@!?\d+>|<@&\d+>", title_text)
    clean_title = re.sub(r"<@!?\d+>|<@&\d+>", "", title_text)
    clean_title = re.sub(r"\s{2,}", " ", clean_title).strip()
    return clean_title or title_text.strip(), mentions


def normalize_text(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").strip().split())


def parse_weekday(value: str) -> int:
    normalized = normalize_text(value)
    weekday_map = {
        "1": 0,
        "\u043f\u043d": 0,
        "\u043f\u043e\u043d\u0435\u0434\u0435\u043b\u044c\u043d\u0438\u043a": 0,
        "monday": 0,
        "mon": 0,
        "2": 1,
        "\u0432\u0442": 1,
        "\u0432\u0442\u043e\u0440\u043d\u0438\u043a": 1,
        "tuesday": 1,
        "tue": 1,
        "3": 2,
        "\u0441\u0440": 2,
        "\u0441\u0440\u0435\u0434\u0430": 2,
        "wednesday": 2,
        "wed": 2,
        "4": 3,
        "\u0447\u0442": 3,
        "\u0447\u0435\u0442\u0432\u0435\u0440\u0433": 3,
        "thursday": 3,
        "thu": 3,
        "5": 4,
        "\u043f\u0442": 4,
        "\u043f\u044f\u0442\u043d\u0438\u0446\u0430": 4,
        "friday": 4,
        "fri": 4,
        "6": 5,
        "\u0441\u0431": 5,
        "\u0441\u0443\u0431\u0431\u043e\u0442\u0430": 5,
        "saturday": 5,
        "sat": 5,
        "7": 6,
        "\u0432\u0441": 6,
        "\u0432\u043e\u0441\u043a\u0440\u0435\u0441\u0435\u043d\u044c\u0435": 6,
        "sunday": 6,
        "sun": 6,
    }
    if normalized not in weekday_map:
        raise ValueError("Invalid weekday. Use 1-7, ru day name, or en day name.")
    return weekday_map[normalized]


def parse_hhmm(value: str) -> str:
    text = value.strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ValueError("Invalid time format. Use HH:MM (for example 19:30).") from exc
    return parsed.strftime("%H:%M")


def weekday_label(index: int) -> str:
    labels = [
        "\u041f\u041d",
        "\u0412\u0422",
        "\u0421\u0420",
        "\u0427\u0422",
        "\u041f\u0422",
        "\u0421\u0411",
        "\u0412\u0421",
    ]
    if index < 0 or index >= len(labels):
        return str(index)
    return labels[index]


def weekday_full_label(index: int) -> str:
    labels = [
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
    ]
    if index < 0 or index >= len(labels):
        return str(index)
    return labels[index]


def _format_vote_users(user_ids: list[int]) -> str:
    if not user_ids:
        return "—"

    mentions = [f"<@{user_id}>" for user_id in user_ids]
    rendered = ", ".join(mentions)
    if len(rendered) <= 1000:
        return rendered

    visible = mentions[:20]
    return ", ".join(visible) + f"\n... и еще {len(mentions) - len(visible)}"


def resolve_next_event_datetime(now_msk: datetime, event_weekday: int, event_time: str) -> datetime:
    hour_text, minute_text = event_time.split(":", maxsplit=1)
    hour = int(hour_text)
    minute = int(minute_text)
    days_ahead = (event_weekday - now_msk.weekday()) % 7
    candidate = (now_msk + timedelta(days=days_ahead)).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if candidate < now_msk:
        candidate += timedelta(days=7)
    return candidate


def build_event_thread_name(title_text: str, event_at_msk: datetime) -> str:
    title = truncate_text(title_text.replace("\n", " "), 55)
    weekday = weekday_full_label(event_at_msk.weekday())
    return truncate_text(f"{weekday} {event_at_msk:%d.%m %H:%M} | {title}", 100)


async def create_event_thread(message: discord.Message, title_text: str, event_at_msk: datetime) -> Optional[int]:
    thread_name = build_event_thread_name(title_text, event_at_msk)
    try:
        thread = await message.create_thread(name=thread_name, auto_archive_duration=10080)
        return thread.id
    except Exception:
        return None


def _upsert_attendance_fields(embed: discord.Embed, going_ids: list[int], not_going_ids: list[int]) -> None:
    kept_fields: list[dict[str, object]] = []
    for field in embed.fields:
        field_name = str(field.name)
        if field_name.startswith("✅ Приду") or field_name.startswith("❌ Не приду"):
            continue
        kept_fields.append(
            {
                "name": field.name,
                "value": field.value,
                "inline": field.inline,
            }
        )

    embed.clear_fields()
    for field in kept_fields:
        embed.add_field(
            name=str(field["name"]),
            value=str(field["value"]),
            inline=bool(field["inline"]),
        )

    embed.add_field(
        name=f"✅ Приду ({len(going_ids)})",
        value=_format_vote_users(going_ids),
        inline=False,
    )
    embed.add_field(
        name=f"❌ Не приду ({len(not_going_ids)})",
        value=_format_vote_users(not_going_ids),
        inline=False,
    )


def build_reminder_embed(
    title_text: str,
    description: str,
    now_msk: datetime,
    event_weekday: int,
    event_time: str,
    going_ids: Optional[list[int]] = None,
    not_going_ids: Optional[list[int]] = None,
) -> discord.Embed:
    going = going_ids or []
    not_going = not_going_ids or []
    clean_title, title_mentions = split_title_mentions(title_text)
    safe_title = truncate_text(clean_title, 256)
    formatted_description = format_reminder_description(description)
    if title_mentions:
        mentions_text = ", ".join(dict.fromkeys(title_mentions))
        formatted_description = f"{formatted_description}\n\nУчастники: {mentions_text}".strip()
    safe_description = truncate_text(formatted_description, 4096)
    embed = discord.Embed(
        title=safe_title,
        description=safe_description if safe_description else None,
        color=discord.Color.blurple(),
    )
    event_at_msk = resolve_next_event_datetime(now_msk, event_weekday, event_time)
    event_weekday_full = weekday_full_label(event_at_msk.weekday())
    embed.add_field(
        name="Начало события (МСК)",
        value=f"`{event_weekday_full}, {event_at_msk:%d.%m}, {event_time}`",
        inline=False,
    )
    _upsert_attendance_fields(embed, going, not_going)
    embed.set_footer(text="LCN Bot")
    return embed


class ReminderVoteStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._votes: dict[str, dict[str, list[int]]] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self._votes = {}
            return

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                self._votes = {}
                return

            parsed: dict[str, dict[str, list[int]]] = {}
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                yes = value.get("yes", [])
                no = value.get("no", [])
                yes_ids = [int(x) for x in yes if isinstance(x, int) or str(x).isdigit()]
                no_ids = [int(x) for x in no if isinstance(x, int) or str(x).isdigit()]
                parsed[str(key)] = {"yes": yes_ids, "no": no_ids}
            self._votes = parsed
        except Exception:
            self._votes = {}

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._votes, f, ensure_ascii=False, indent=2)

    def ensure_message(self, message_id: int) -> None:
        key = str(message_id)
        if key not in self._votes:
            self._votes[key] = {"yes": [], "no": []}
            self.save()

    def reset_message(self, message_id: int) -> None:
        self._votes[str(message_id)] = {"yes": [], "no": []}
        self.save()

    def set_vote(self, message_id: int, user_id: int, vote: str) -> tuple[list[int], list[int]]:
        key = str(message_id)
        if key not in self._votes:
            self._votes[key] = {"yes": [], "no": []}

        record = self._votes[key]
        yes = [uid for uid in record.get("yes", []) if uid != user_id]
        no = [uid for uid in record.get("no", []) if uid != user_id]

        if vote == "yes":
            yes.append(user_id)
        elif vote == "no":
            no.append(user_id)
        else:
            raise ValueError("Unknown vote type")

        record["yes"] = yes
        record["no"] = no
        self.save()
        return yes, no

    def get_votes(self, message_id: int) -> tuple[list[int], list[int]]:
        key = str(message_id)
        record = self._votes.get(key, {"yes": [], "no": []})
        yes = [int(uid) for uid in record.get("yes", [])]
        no = [int(uid) for uid in record.get("no", [])]
        return yes, no


class ReminderDispatchStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._items: dict[str, dict[str, object]] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self._items = {}
            return

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._items = {str(k): v for k, v in data.items() if isinstance(v, dict)}
            else:
                self._items = {}
        except Exception:
            self._items = {}

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False, indent=2)

    def register(self, message_id: int, reminder_id: int, event_at_msk: datetime) -> None:
        self._items[str(message_id)] = {
            "message_id": message_id,
            "reminder_id": reminder_id,
            "event_at": event_at_msk.strftime("%Y-%m-%d %H:%M"),
            "dm_sent": False,
        }
        self.save()

    def mark_dm_sent(self, message_id: int) -> None:
        key = str(message_id)
        if key not in self._items:
            return
        self._items[key]["dm_sent"] = True
        self.save()

    def due_pre_event_dm(self, now_msk: datetime) -> list[dict[str, object]]:
        due: list[dict[str, object]] = []
        for item in self._items.values():
            if bool(item.get("dm_sent", False)):
                continue
            event_at_raw = str(item.get("event_at", ""))
            if not event_at_raw:
                continue
            try:
                event_at = datetime.strptime(event_at_raw, "%Y-%m-%d %H:%M").replace(tzinfo=MSK_TZ)
            except ValueError:
                continue
            pre_event_at = event_at - timedelta(minutes=30)
            if now_msk >= pre_event_at and now_msk < event_at:
                due.append(item)
        return due

    def cleanup_old(self, now_msk: datetime) -> None:
        to_delete: list[str] = []
        for key, item in self._items.items():
            event_at_raw = str(item.get("event_at", ""))
            if not event_at_raw:
                to_delete.append(key)
                continue
            try:
                event_at = datetime.strptime(event_at_raw, "%Y-%m-%d %H:%M").replace(tzinfo=MSK_TZ)
            except ValueError:
                to_delete.append(key)
                continue

            if now_msk >= event_at + timedelta(days=2):
                to_delete.append(key)

        if not to_delete:
            return

        for key in to_delete:
            self._items.pop(key, None)
        self.save()


class ReminderDmLinkStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._items: dict[str, dict[str, int]] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self._items = {}
            return

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                self._items = {}
                return

            parsed: dict[str, dict[str, int]] = {}
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                public_message_id = int(value.get("public_message_id", 0))
                public_channel_id = int(value.get("public_channel_id", 0))
                if public_message_id > 0 and public_channel_id > 0:
                    parsed[str(key)] = {
                        "public_message_id": public_message_id,
                        "public_channel_id": public_channel_id,
                    }
            self._items = parsed
        except Exception:
            self._items = {}

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False, indent=2)

    def register(self, dm_message_id: int, public_message_id: int, public_channel_id: int) -> None:
        self._items[str(dm_message_id)] = {
            "public_message_id": public_message_id,
            "public_channel_id": public_channel_id,
        }
        self.save()

    def get_public_message(self, dm_message_id: int) -> Optional[dict[str, int]]:
        return self._items.get(str(dm_message_id))


class ReminderVoteView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _handle_vote(self, interaction: discord.Interaction, vote: str) -> None:
        if interaction.message is None:
            if not interaction.response.is_done():
                await interaction.response.send_message("Не удалось определить сообщение.", ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=False)

        message_id = interaction.message.id
        user_id = interaction.user.id

        yes_ids, no_ids = reminder_votes.set_vote(message_id, user_id, vote)

        if interaction.message.embeds:
            base_embed = discord.Embed.from_dict(interaction.message.embeds[0].to_dict())
        else:
            base_embed = discord.Embed(
                title=(interaction.message.content or "Напоминание"),
                color=discord.Color.blurple(),
            )
            base_embed.set_footer(text="LCN Bot")

        _upsert_attendance_fields(base_embed, yes_ids, no_ids)

        try:
            await interaction.message.edit(embed=base_embed, view=self)
        except Exception:
            pass

        result_text = "Приду" if vote == "yes" else "Не приду"
        await interaction.followup.send(f"Твой выбор: `{result_text}`", ephemeral=True)

    @discord.ui.button(label="Приду", style=discord.ButtonStyle.success, emoji="✅", custom_id="reminder_vote_yes")
    async def vote_yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._handle_vote(interaction, "yes")

    @discord.ui.button(label="Не приду", style=discord.ButtonStyle.danger, emoji="❌", custom_id="reminder_vote_no")
    async def vote_no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._handle_vote(interaction, "no")


class ReminderDmSignupView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _handle_vote(self, interaction: discord.Interaction, vote: str) -> None:
        if interaction.message is None:
            if not interaction.response.is_done():
                await interaction.response.send_message("Не удалось определить сообщение.", ephemeral=True)
            return

        link = reminder_dm_links.get_public_message(interaction.message.id)
        if link is None:
            await interaction.response.send_message(
                "Не нашёл связанное объявление. Попробуй проголосовать в канале расписания.",
                ephemeral=True,
            )
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=False)

        public_message_id = int(link["public_message_id"])
        public_channel_id = int(link["public_channel_id"])
        yes_ids, no_ids = reminder_votes.set_vote(public_message_id, interaction.user.id, vote)

        channel = bot.get_channel(public_channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(public_channel_id)
            except Exception:
                channel = None

        if channel is not None:
            try:
                public_message = await channel.fetch_message(public_message_id)
                if public_message.embeds:
                    embed = discord.Embed.from_dict(public_message.embeds[0].to_dict())
                else:
                    embed = discord.Embed(title=(public_message.content or "Напоминание"))
                    embed.set_footer(text="LCN Bot")
                _upsert_attendance_fields(embed, yes_ids, no_ids)
                await public_message.edit(embed=embed, view=ReminderVoteView())
            except Exception:
                pass

        result_text = "Приду" if vote == "yes" else "Не приду"
        await interaction.followup.send(f"Твой выбор синхронизирован с расписанием: `{result_text}`", ephemeral=True)

    @discord.ui.button(label="Приду", style=discord.ButtonStyle.success, emoji="✅", custom_id="reminder_dm_vote_yes")
    async def vote_yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._handle_vote(interaction, "yes")

    @discord.ui.button(label="Не приду", style=discord.ButtonStyle.danger, emoji="❌", custom_id="reminder_dm_vote_no")
    async def vote_no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._handle_vote(interaction, "no")


async def send_signup_dm_to_role_members(
    guild: Optional[discord.Guild],
    role_id: int,
    public_message: discord.Message,
    title_text: str,
    event_at_msk: datetime,
) -> None:
    if guild is None or role_id <= 0:
        return

    role = guild.get_role(role_id)
    if role is None:
        return

    members = [member for member in role.members if not member.bot]
    if not members:
        return

    event_label = f"{weekday_full_label(event_at_msk.weekday())}, {event_at_msk:%d.%m}, {event_at_msk:%H:%M} МСК"
    jump_url = public_message.jump_url
    text = (
        f"Привет. Открылась запись на игру `{title_text}`.\n"
        f"Начало: `{event_label}`.\n"
        f"Расписание: {jump_url}\n\n"
        "Нажми кнопку ниже, и я синхронизирую твой выбор с каналом расписания."
    )

    for member in members:
        try:
            dm_message = await member.send(text, view=ReminderDmSignupView())
            reminder_dm_links.register(dm_message.id, public_message.id, public_message.channel.id)
        except Exception:
            continue


def _reminder_option_label(item: dict[str, object]) -> str:
    reminder_id = int(item.get("id", 0))
    event_weekday = int(item.get("weekday", -1))
    event_time = str(item.get("time", ""))
    title_text = str(item.get("title", "")).strip() or str(item.get("message", "")).strip() or "Без названия"
    short_title = truncate_cell(title_text, 60)
    return f"#{reminder_id} {weekday_label(event_weekday)} {event_time} | {short_title}"


class ReminderDeleteSelect(discord.ui.Select):
    def __init__(self, items: list[dict[str, object]]) -> None:
        options: list[discord.SelectOption] = []
        for item in items[:25]:
            reminder_id = int(item.get("id", 0))
            options.append(
                discord.SelectOption(
                    label=_reminder_option_label(item),
                    value=str(reminder_id),
                )
            )

        super().__init__(
            placeholder="Выбери напоминание для удаления",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="reminder_delete_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.values:
            await interaction.response.send_message("Не выбрано напоминание.", ephemeral=True)
            return

        reminder_id = int(self.values[0])
        deleted = reminders.remove(reminder_id)
        if not deleted:
            await interaction.response.send_message(
                f"Напоминание с id `{reminder_id}` не найдено.",
                ephemeral=True,
            )
            return

        self.disabled = True
        if self.view is not None:
            for child in self.view.children:
                child.disabled = True

        await interaction.response.edit_message(
            content=f"Напоминание `{reminder_id}` удалено.",
            view=self.view,
        )


class ReminderDeleteView(discord.ui.View):
    def __init__(self, items: list[dict[str, object]]) -> None:
        super().__init__(timeout=120)
        self.add_item(ReminderDeleteSelect(items))


async def send_interaction_text_chunks(interaction: discord.Interaction, text: str) -> None:
    chunks: list[str] = []
    if len(text) <= MAX_DISCORD_MESSAGE_LEN:
        chunks = [text]
    else:
        lines = text.splitlines()
        chunk = ""
        for line in lines:
            candidate = f"{chunk}\n{line}".strip("\n")
            if len(candidate) > MAX_DISCORD_MESSAGE_LEN and chunk:
                chunks.append(chunk)
                chunk = line
            else:
                chunk = candidate
        if chunk:
            chunks.append(chunk)

    for idx, chunk in enumerate(chunks):
        if idx == 0 and not interaction.response.is_done():
            await interaction.response.send_message(chunk)
        else:
            await interaction.followup.send(chunk)



class ReminderManager:
    def __init__(self, path: str) -> None:
        self.path = path
        self._items: list[dict[str, object]] = []
        self._next_id = 1
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self._items = []
            self._next_id = 1
            return

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", []) if isinstance(data, dict) else []
            self._items = [item for item in items if isinstance(item, dict)]
            max_id = max((int(item.get("id", 0)) for item in self._items), default=0)
            self._next_id = max_id + 1
        except Exception:
            self._items = []
            self._next_id = 1

    def save(self) -> None:
        payload = {"items": self._items}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def add(
        self,
        weekday: int,
        hhmm: str,
        channel_id: int,
        title: str,
        description: str = "",
        remind_weekday: Optional[int] = None,
        remind_hhmm: Optional[str] = None,
        role_id: int = 0,
        repeat: bool = True,
    ) -> dict[str, object]:
        item = {
            "id": self._next_id,
            "weekday": weekday,
            "time": hhmm,
            "remind_weekday": remind_weekday if remind_weekday is not None else weekday,
            "remind_time": remind_hhmm or hhmm,
            "channel_id": channel_id,
            "role_id": role_id,
            "title": title,
            "description": description,
            "repeat": repeat,
            "last_trigger_key": "",
        }
        self._next_id += 1
        self._items.append(item)
        self.save()
        return item

    def all(self) -> list[dict[str, object]]:
        return list(self._items)

    def get_by_id(self, reminder_id: int) -> Optional[dict[str, object]]:
        for item in self._items:
            if int(item.get("id", 0)) == reminder_id:
                return item
        return None

    def update(self, reminder_id: int, updates: dict[str, object]) -> Optional[dict[str, object]]:
        for item in self._items:
            if int(item.get("id", 0)) != reminder_id:
                continue
            item.update(updates)
            if any(key in updates for key in ("weekday", "time", "remind_weekday", "remind_time")):
                item["last_trigger_key"] = ""
            self.save()
            return item
        return None

    def remove(self, reminder_id: int) -> bool:
        original_len = len(self._items)
        self._items = [item for item in self._items if int(item.get("id", 0)) != reminder_id]
        if len(self._items) == original_len:
            return False
        self.save()
        return True

    def mark_triggered(self, reminder_id: int, trigger_key: str) -> None:
        for item in self._items:
            if int(item.get("id", 0)) == reminder_id:
                item["last_trigger_key"] = trigger_key
                self.save()
                return

    def due(self, now_msk: datetime) -> list[dict[str, object]]:
        key = now_msk.strftime("%Y-%m-%d %H:%M")
        result: list[dict[str, object]] = []
        for item in self._items:
            publish_weekday = int(item.get("remind_weekday", item.get("weekday", -1)))
            publish_time = str(item.get("remind_time", item.get("time", "")))
            if publish_weekday != now_msk.weekday():
                continue
            if publish_time != now_msk.strftime("%H:%M"):
                continue
            if str(item.get("last_trigger_key", "")) == key:
                continue
            result.append(item)
        return result


async def reminder_worker() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        now_msk = datetime.now(MSK_TZ)
        due_items = reminders.due(now_msk)
        for item in due_items:
            channel_id = int(item.get("channel_id", 0))
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    reminders.mark_triggered(int(item.get("id", 0)), now_msk.strftime("%Y-%m-%d %H:%M"))
                    continue

            title_text = (
                str(item.get("title", "")).strip()
                or str(item.get("message", "")).strip()
                or "Напоминание: скоро ивент. Запишитесь, пожалуйста."
            )
            description = str(item.get("description", "")).strip()
            event_weekday = int(item.get("weekday", now_msk.weekday()))
            event_time = str(item.get("time", now_msk.strftime("%H:%M")))
            embed = build_reminder_embed(title_text, description, now_msk, event_weekday, event_time)
            view = ReminderVoteView()
            role_id = int(item.get("role_id", 0) or 0)
            ping_text = f"<@&{role_id}>" if role_id > 0 else None
            allowed_mentions = discord.AllowedMentions(roles=True)
            try:
                sent_message = await channel.send(
                    content=ping_text,
                    embed=embed,
                    view=view,
                    allowed_mentions=allowed_mentions,
                )
                reminder_votes.reset_message(sent_message.id)
                event_at_msk = resolve_next_event_datetime(now_msk, event_weekday, event_time)
                await create_event_thread(sent_message, title_text, event_at_msk)
                await send_signup_dm_to_role_members(
                    getattr(channel, "guild", None),
                    role_id,
                    sent_message,
                    title_text,
                    event_at_msk,
                )
                reminder_dispatches.register(sent_message.id, int(item.get("id", 0)), event_at_msk)
            except Exception:
                try:
                    if description:
                        fallback_text = f"{title_text}\nОписание: {description}"
                    else:
                        fallback_text = title_text
                    if ping_text:
                        fallback_text = f"{ping_text}\n{fallback_text}"
                    sent_message = await channel.send(
                        fallback_text,
                        view=view,
                        allowed_mentions=allowed_mentions,
                    )
                    reminder_votes.reset_message(sent_message.id)
                    event_at_msk = resolve_next_event_datetime(now_msk, event_weekday, event_time)
                    await create_event_thread(sent_message, title_text, event_at_msk)
                    await send_signup_dm_to_role_members(
                        getattr(channel, "guild", None),
                        role_id,
                        sent_message,
                        title_text,
                        event_at_msk,
                    )
                    reminder_dispatches.register(sent_message.id, int(item.get("id", 0)), event_at_msk)
                except Exception:
                    pass
            finally:
                reminder_id = int(item.get("id", 0))
                if bool(item.get("repeat", True)):
                    reminders.mark_triggered(reminder_id, now_msk.strftime("%Y-%m-%d %H:%M"))
                else:
                    reminders.remove(reminder_id)

        due_dm_items = reminder_dispatches.due_pre_event_dm(now_msk)
        for dispatch_item in due_dm_items:
            message_id = int(dispatch_item.get("message_id", 0))
            reminder_id = int(dispatch_item.get("reminder_id", 0))
            yes_ids, _ = reminder_votes.get_votes(message_id)

            reminder_item = reminders.get_by_id(reminder_id) or {}
            title_text = (
                str(reminder_item.get("title", "")).strip()
                or str(reminder_item.get("message", "")).strip()
                or "Скоро событие"
            )
            description = str(reminder_item.get("description", "")).strip()
            event_time = str(reminder_item.get("time", now_msk.strftime("%H:%M")))
            event_weekday = int(reminder_item.get("weekday", now_msk.weekday()))
            event_label = f"{weekday_label(event_weekday)} {event_time} (МСК)"

            for user_id in yes_ids:
                user = bot.get_user(user_id)
                if user is None:
                    try:
                        user = await bot.fetch_user(user_id)
                    except Exception:
                        continue

                dm_text = f"Напоминание: через 30 минут стартует событие `{title_text}`.\nВремя: `{event_label}`."
                if description:
                    dm_text += f"\nОписание: {description}"
                try:
                    await user.send(dm_text)
                except Exception:
                    continue

            reminder_dispatches.mark_dm_sent(message_id)

        reminder_dispatches.cleanup_old(now_msk)
        await asyncio.sleep(20)
load_dotenv(encoding="utf-8-sig")

TOKEN = require_env("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = optional_int_env("DISCORD_GUILD_ID", 0)

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
commands_synced = False
reminders = ReminderManager(REMINDERS_FILE)
reminder_votes = ReminderVoteStore(REMINDER_VOTES_FILE)
reminder_dispatches = ReminderDispatchStore(REMINDER_DISPATCHES_FILE)
reminder_dm_links = ReminderDmLinkStore(REMINDER_DM_LINKS_FILE)
reminder_task: Optional[asyncio.Task] = None
vote_view_registered = False



@bot.event
async def on_ready() -> None:
    global commands_synced, reminder_task, vote_view_registered

    if not commands_synced:
        synced_global = await bot.tree.sync()
        print(f"Synced global commands: {len(synced_global)}")
        if DISCORD_GUILD_ID > 0:
            guild_obj = discord.Object(id=DISCORD_GUILD_ID)
            synced_guild = await bot.tree.sync(guild=guild_obj)
            print(f"Synced guild commands ({DISCORD_GUILD_ID}): {len(synced_guild)}")
        commands_synced = True

    if not vote_view_registered:
        bot.add_view(ReminderVoteView())
        bot.add_view(ReminderDmSignupView())
        vote_view_registered = True

    now_msk = datetime.now(MSK_TZ)
    print(f"Logged in as {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")
    print(f"Current MSK time: {now_msk:%Y-%m-%d %H:%M:%S}")

    if reminder_task is None or reminder_task.done():
        reminder_task = asyncio.create_task(reminder_worker())



@bot.tree.command(name="add_reminder", description="Add weekly reminder by Moscow time")
@app_commands.describe(
    weekday="Event weekday (1-7, ru/en name; e.g. 5 or пятница)",
    time="Event start time (MSK) HH:MM",
    remind_weekday="When to publish reminder: weekday (optional, default = event weekday)",
    remind_time="When to publish reminder: time HH:MM (optional, default = event time)",
    ping_role="Server role to ping when reminder is posted (optional)",
    repeat="Publish every week if true, publish once if false",
    title="Reminder title (optional)",
    description="Event description (optional)",
)
async def add_reminder(
    interaction: discord.Interaction,
    weekday: str,
    time: str,
    remind_weekday: Optional[str] = None,
    remind_time: Optional[str] = None,
    ping_role: Optional[discord.Role] = None,
    repeat: bool = True,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        weekday_index = parse_weekday(weekday)
        event_hhmm = parse_hhmm(time)
        publish_weekday_index = parse_weekday(remind_weekday) if remind_weekday else weekday_index
        publish_hhmm = parse_hhmm(remind_time) if remind_time else event_hhmm
        reminder_title = (title or "Напоминание: скоро ивент. Запишитесь, пожалуйста.").strip()
        reminder_description = (description or "").strip()
        reminder = reminders.add(
            weekday=weekday_index,
            hhmm=event_hhmm,
            channel_id=interaction.channel_id,
            title=reminder_title,
            description=reminder_description,
            remind_weekday=publish_weekday_index,
            remind_hhmm=publish_hhmm,
            role_id=ping_role.id if ping_role else 0,
            repeat=repeat,
        )
        ping_info = f"\nPing role: `{ping_role.name}`" if ping_role else ""
        repeat_info = "weekly" if repeat else "once"
        await interaction.followup.send(
            f"Reminder added: id `{reminder['id']}`.\n"
            f"Event: `{weekday_label(weekday_index)} {event_hhmm}` (MSK)\n"
            f"Publish: `{weekday_label(publish_weekday_index)} {publish_hhmm}` (MSK), channel `{interaction.channel_id}`.\n"
            f"Mode: `{repeat_info}`"
            f"{ping_info}",
            ephemeral=True,
        )
    except ValueError as exc:
        await interaction.followup.send(f"Failed to add reminder: {exc}", ephemeral=True)
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Failed to add reminder: {exc}", ephemeral=True)


@bot.tree.command(name="list_reminders", description="List configured reminders (Moscow time)")
async def list_reminders(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)

    items = reminders.all()
    if not items:
        await interaction.followup.send("No reminders configured.", ephemeral=True)
        return

    now_msk = datetime.now(MSK_TZ)
    blocks = [f"## Reminders (MSK)\nВсего: `{len(items)}`"]
    for item in items:
        event_weekday = int(item.get("weekday", -1))
        event_time = str(item.get("time", ""))
        title_text = str(item.get("title", "")).strip() or str(item.get("message", "")).strip()
        try:
            event_at_msk = resolve_next_event_datetime(now_msk, event_weekday, event_time)
            event_date = f"{weekday_full_label(event_at_msk.weekday())}, {event_at_msk:%d.%m.%Y %H:%M}"
        except Exception:
            event_date = f"{weekday_label(event_weekday)} {event_time}"
        blocks.append(
            "\n".join(
                [
                    f"ID `{item.get('id')}`",
                    f"Название: {truncate_text(title_text, 250) or '-'}",
                    f"Дата: `{event_date}`",
                ]
            )
        )

    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}".strip()
        if len(candidate) > MAX_DISCORD_MESSAGE_LEN and current:
            await send_interaction_text_chunks(interaction, current)
            current = block
        else:
            current = candidate

    if current:
        await send_interaction_text_chunks(interaction, current)


@bot.tree.command(name="edit_reminder", description="Edit reminder by id")
@app_commands.describe(
    reminder_id="Reminder id from /list_reminders",
    weekday="New event weekday (1-7, ru/en)",
    time="New event start time (MSK) HH:MM",
    remind_weekday="New publish weekday (1-7, ru/en)",
    remind_time="New publish time HH:MM",
    ping_role="New server role to ping",
    clear_ping_role="Remove role ping if true",
    repeat="Publish every week if true, publish once if false",
    title="New reminder title",
    description="New event description",
)
async def edit_reminder(
    interaction: discord.Interaction,
    reminder_id: int,
    weekday: Optional[str] = None,
    time: Optional[str] = None,
    remind_weekday: Optional[str] = None,
    remind_time: Optional[str] = None,
    ping_role: Optional[discord.Role] = None,
    clear_ping_role: bool = False,
    repeat: Optional[bool] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    await interaction.response.defer(thinking=True)

    if reminder_id < 1:
        await interaction.followup.send("Reminder id must be >= 1.")
        return

    updates: dict[str, object] = {}
    try:
        if weekday:
            updates["weekday"] = parse_weekday(weekday)
        if time:
            updates["time"] = parse_hhmm(time)
        if remind_weekday:
            updates["remind_weekday"] = parse_weekday(remind_weekday)
        if remind_time:
            updates["remind_time"] = parse_hhmm(remind_time)
    except ValueError as exc:
        await interaction.followup.send(f"Failed to edit reminder: {exc}")
        return

    if ping_role is not None and clear_ping_role:
        await interaction.followup.send("Choose either `ping_role` or `clear_ping_role`, not both.")
        return
    if ping_role is not None:
        updates["role_id"] = ping_role.id
    if clear_ping_role:
        updates["role_id"] = 0
    if repeat is not None:
        updates["repeat"] = repeat
    if title is not None:
        updates["title"] = title.strip()
    if description is not None:
        updates["description"] = description.strip()

    if not updates:
        await interaction.followup.send("Nothing to update. Add at least one field.")
        return

    updated = reminders.update(reminder_id, updates)
    if updated is None:
        await interaction.followup.send(f"Reminder with id `{reminder_id}` not found.")
        return

    event_weekday = int(updated.get("weekday", -1))
    event_time = str(updated.get("time", ""))
    publish_weekday = int(updated.get("remind_weekday", event_weekday))
    publish_time = str(updated.get("remind_time", event_time))
    role_id = int(updated.get("role_id", 0) or 0)
    mode = "weekly" if bool(updated.get("repeat", True)) else "once"
    title_text = str(updated.get("title", "")).strip() or str(updated.get("message", "")).strip()

    await interaction.followup.send(
        f"Reminder `{reminder_id}` updated.\n"
        f"Event: `{weekday_label(event_weekday)} {event_time}` (MSK)\n"
        f"Publish: `{weekday_label(publish_weekday)} {publish_time}` (MSK)\n"
        f"Mode: `{mode}`\n"
        f"Ping role: {f'<@&{role_id}>' if role_id > 0 else '-'}\n"
        f"Title: `{truncate_cell(title_text, 120) or '-'}`"
    )


@bot.tree.command(name="delete_reminder", description="Delete reminder by id")
@app_commands.describe(reminder_id="Reminder id from /list_reminders")
async def delete_reminder(interaction: discord.Interaction, reminder_id: int) -> None:
    await interaction.response.defer(thinking=True)

    if reminder_id < 1:
        await interaction.followup.send("Reminder id must be >= 1.")
        return

    deleted = reminders.remove(reminder_id)
    if not deleted:
        await interaction.followup.send(f"Reminder with id `{reminder_id}` not found.")
        return

    await interaction.followup.send(f"Reminder `{reminder_id}` deleted.")


@bot.tree.command(name="delete_reminder_list", description="Delete reminder from interactive list")
async def delete_reminder_list(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)

    items = reminders.all()
    if not items:
        await interaction.followup.send("Список напоминаний пуст.", ephemeral=True)
        return

    view = ReminderDeleteView(items)
    await interaction.followup.send(
        "Выбери напоминание для удаления:",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("pong")


if __name__ == "__main__":
    bot.run(TOKEN)







