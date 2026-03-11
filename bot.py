import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
import gspread
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
MAX_DISCORD_MESSAGE_LEN = 1900
try:
    MSK_TZ = ZoneInfo("Europe/Moscow")
except Exception:
    MSK_TZ = timezone(timedelta(hours=3), name="MSK")
REMINDERS_FILE = "reminders.json"
REMINDER_VOTES_FILE = "reminder_votes.json"
REMINDER_DISPATCHES_FILE = "reminder_dispatches.json"


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


def normalize_text(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").strip().split())


def is_rotation_one_text(value: str) -> bool:
    normalized = normalize_text(value)
    return re.search(r"ротация\s*№?\s*1(\D|$)", normalized) is not None


def side_matches(requested_side: str, actual_side: str) -> bool:
    requested = normalize_text(requested_side)
    actual = normalize_text(actual_side)
    if not requested or not actual:
        return False

    if requested == actual or requested in actual:
        return True

    if requested in {"1", "2", "3", "4"} and f"сторона {requested}" in actual:
        return True

    return False



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
    event_weekday: int,
    event_time: str,
    going_ids: Optional[list[int]] = None,
    not_going_ids: Optional[list[int]] = None,
) -> discord.Embed:
    going = going_ids or []
    not_going = not_going_ids or []
    embed = discord.Embed(
        title=title_text,
        color=discord.Color.blurple(),
    )
    if description:
        embed.add_field(
            name="Описание",
            value=description,
            inline=False,
        )
    embed.add_field(
        name="Начало события (МСК)",
        value=f"`{weekday_label(event_weekday)} {event_time}`",
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


def ascii_box(title: str, width: int = 46) -> str:
    text = truncate_cell(title, max_len=width).strip() or "-"
    line = text.center(width)
    top = "+" + "-" * (width + 2) + "+"
    mid = f"| {line} |"
    bot = "+" + "-" * (width + 2) + "+"
    return "\n".join([top, mid, bot])


def ascii_box_lines(lines: list[str], width: int = 78) -> str:
    top = "+" + "-" * (width + 2) + "+"
    body: list[str] = []
    for line in lines:
        text = truncate_cell(line, max_len=width)
        body.append(f"| {text.ljust(width)} |")
    bot = "+" + "-" * (width + 2) + "+"
    return "\n".join([top, *body, bot])


def column_index_to_label(index: int) -> str:
    if index < 1:
        raise ValueError("Column index must be >= 1")

    label = ""
    current = index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        label = chr(65 + remainder) + label
    return label


def parse_column_input(raw_column: str) -> int:
    value = raw_column.strip().upper()
    if not value:
        raise ValueError("Column is empty")

    if value.isdigit():
        column = int(value)
        if column < 1:
            raise ValueError("Column number must be >= 1")
        return column

    if not value.isalpha():
        raise ValueError("Column must be a number (3) or letters (C)")

    column = 0
    for char in value:
        column = column * 26 + (ord(char) - 64)
    return column


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


class SheetsWriter:
    def __init__(self) -> None:
        creds_file = require_env("GOOGLE_SERVICE_ACCOUNT_FILE")
        spreadsheet_id = require_env("GOOGLE_SHEET_ID")
        worksheet_name = require_env("GOOGLE_WORKSHEET_NAME")
        self.clan_name = require_env("CLAN_NAME")
        self.target_column = optional_int_env("TARGET_COLUMN", 0)
        self.start_row = optional_int_env("START_ROW", 2)
        self.preview_max_rows = optional_int_env("TABLE_PREVIEW_MAX_ROWS", 15)
        self.preview_max_cols = optional_int_env("TABLE_PREVIEW_MAX_COLS", 6)

        credentials = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
        client = gspread.authorize(credentials)
        self.worksheet = client.open_by_key(spreadsheet_id).worksheet(worksheet_name)

    def _first_empty_row(self, column: int) -> int:
        values = self.worksheet.col_values(column)
        if len(values) < self.start_row:
            return self.start_row

        for row_index in range(self.start_row, len(values) + 1):
            current = values[row_index - 1].strip() if row_index - 1 < len(values) else ""
            if not current:
                return row_index
        return len(values) + 1

    def write_clan(self, column: int, row: Optional[int] = None) -> int:
        target_row = row or self._first_empty_row(column)
        self.worksheet.update_cell(target_row, column, self.clan_name)
        return target_row

    def write_occupied(self, row: int, count: int) -> tuple[str, int]:
        entries = self._extract_assignment_entries(self.worksheet.get_all_values())
        match = next((entry for entry in entries if int(entry["row"]) == row), None)
        if match is None:
            raise ValueError("Row is not part of a detected squads/slots table")

        occupied_col = int(match["occupied_col"])
        self.worksheet.update_cell(row, occupied_col, str(count))
        return str(match["squad_tag"]), occupied_col

    def write_assignment_for_row(
        self,
        row: int,
        count: int,
        side: Optional[str] = None,
    ) -> tuple[str, int, int, str]:
        entries = self._extract_assignment_entries(self.worksheet.get_all_values())
        row_matches = [entry for entry in entries if int(entry["row"]) == row]
        if not row_matches:
            raise ValueError("Row is not part of a detected squads/slots table")

        matches = row_matches
        if side:
            matches = [entry for entry in row_matches if side_matches(side, str(entry["side"]))]
            if not matches:
                available_sides = ", ".join(sorted({str(entry["side"]) for entry in row_matches}))
                raise ValueError(
                    f"Row {row} has no match for side '{side}'. Available: {available_sides}"
                )

        if len(matches) > 1:
            available_sides = ", ".join(sorted({str(entry["side"]) for entry in matches}))
            raise ValueError(
                f"Row {row} matches multiple sides ({available_sides}). Add side: `/occupy row:{row} count:{count} side:1`"
            )

        match = matches[0]

        squad_col = int(match["squad_col"])
        occupied_col = int(match["occupied_col"])
        self.worksheet.update_cell(row, squad_col, self.clan_name)
        self.worksheet.update_cell(row, occupied_col, str(count))
        return self.clan_name, squad_col, occupied_col, str(match["side"])

    def render_preview(self) -> str:
        values = self.worksheet.get_all_values()
        if not values:
            return f"Worksheet `{self.worksheet.title}` is empty."

        assignments_preview = self._render_assignments_preview(values)
        if assignments_preview is not None:
            return assignments_preview

        visible_rows = values[: self.preview_max_rows]
        max_present_cols = max((len(row) for row in visible_rows), default=0)
        visible_col_count = min(max_present_cols, self.preview_max_cols)

        if visible_col_count == 0:
            return f"Worksheet `{self.worksheet.title}` has no visible values."

        row_blocks: list[str] = []
        for row_index, row_values in enumerate(visible_rows, start=1):
            cell_parts: list[str] = []
            for col in range(1, visible_col_count + 1):
                raw_value = row_values[col - 1] if col - 1 < len(row_values) else ""
                value = truncate_cell(raw_value, max_len=32)
                if value:
                    cell_parts.append(f"{column_index_to_label(col)}: {value}")

            if not cell_parts:
                row_blocks.append(f"**{row_index}.** _(empty)_")
                continue

            row_blocks.append(f"**{row_index}.** " + " | ".join(cell_parts))

        shown_columns = ", ".join(column_index_to_label(i) for i in range(1, visible_col_count + 1))
        result = [
            f"## Sheet Preview: `{self.worksheet.title}`",
            f"Shown columns: `{shown_columns}`",
            "",
            *row_blocks,
        ]
        if len(values) > self.preview_max_rows or max_present_cols > self.preview_max_cols:
            result.append(
                ""
            )
            result.append(
                f"Preview is limited to {self.preview_max_rows} rows and {self.preview_max_cols} columns. "
                "Use column letters/numbers from the preview for `/signup`."
            )
        return "\n".join(result)

    def render_preview_messages(self) -> list[str]:
        values = self.worksheet.get_all_values()
        if not values:
            return [f"Worksheet `{self.worksheet.title}` is empty."]

        assignment_messages = self._render_assignments_preview_messages(values)
        if assignment_messages:
            return assignment_messages

        return [self.render_preview()]

    def render_unselected_preview_messages(self) -> list[str]:
        values = self.worksheet.get_all_values()
        if not values:
            return [f"Worksheet `{self.worksheet.title}` is empty."]

        entries = self._extract_assignment_entries(values)
        if not entries:
            return ["Не удалось найти таблицы отрядов/слотов."]

        filtered = [
            entry
            for entry in entries
            if normalize_text(str(entry["squad_tag"])) == "не выбран"
        ]
        if not filtered:
            return ["Строки с `Отряд = Не выбран` не найдены."]

        shown_entries = filtered[: self.preview_max_rows]
        grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
        order: list[tuple[str, str]] = []
        for entry in shown_entries:
            event = str(entry["event"]).strip() or "Ротация 1"
            side = str(entry["side"]).strip() or "Сторона"
            key = (event, side)
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(entry)

        messages: list[str] = []
        for event, side in order:
            lines: list[str] = [
                f"## Не выбран: `{self.worksheet.title}`",
                f"**{event}**",
                f"`{side}`",
                "",
            ]

            for entry in grouped[(event, side)]:
                vehicle_text = truncate_cell(str(entry["vehicle"]), 72) or "-"
                squad_tag_text = truncate_cell(str(entry["squad_tag"]), 20) or "-"
                occupied_text = truncate_cell(str(entry["occupied"]), 8) or "-"
                slots_text = truncate_cell(str(entry["slots"]), 8) or "-"
                squad_no_text = truncate_cell(str(entry["squad_no"]), 26) or "-"
                lines.append(
                    f"Строка `{entry['row']}` | `{squad_no_text}` | Техника `{vehicle_text}`"
                )
                lines.append(
                    f"Слоты `{slots_text}` | Отряд `{squad_tag_text}` | Занято `{occupied_text}`"
                )
                lines.append("")

            while lines and lines[-1] == "":
                lines.pop()
            messages.append("\n".join(lines))

        if len(filtered) > len(shown_entries):
            messages.append(
                f"Показано {len(shown_entries)} из {len(filtered)} строк с `Не выбран`. "
                "Увеличь `TABLE_PREVIEW_MAX_ROWS`, если нужно больше."
            )

        return messages

    def _find_assignment_tables(self, values: list[list[str]]) -> list[dict[str, object]]:
        tables: list[dict[str, object]] = []
        seen: set[tuple[int, int]] = set()

        for row_idx, row in enumerate(values, start=1):
            normalized_row = [normalize_text(cell) for cell in row]
            for col_idx, cell_text in enumerate(normalized_row, start=1):
                if "№ отделения" not in cell_text and "no отделения" not in cell_text:
                    continue

                search_end = min(len(normalized_row), col_idx + 8)
                vehicle_col: Optional[int] = None
                slots_col: Optional[int] = None
                squad_col: Optional[int] = None
                occupied_col: Optional[int] = None

                for probe_col in range(col_idx, search_end + 1):
                    probe = normalized_row[probe_col - 1]
                    if "техника" in probe:
                        vehicle_col = probe_col
                    elif "слоты" in probe:
                        slots_col = probe_col
                    elif "отряд" in probe:
                        squad_col = probe_col
                    elif "занято" in probe:
                        occupied_col = probe_col

                if not all([vehicle_col, slots_col, squad_col, occupied_col]):
                    continue

                key = (row_idx, col_idx)
                if key in seen:
                    continue
                seen.add(key)

                side_label = "Сторона"
                rotation_label = ""
                operation_label = ""
                left_bound = max(1, col_idx - 2)
                right_bound = max(int(occupied_col), int(slots_col))
                right_bound = min(right_bound + 2, max((len(r) for r in values), default=right_bound + 2))

                for back_row in range(row_idx - 1, max(0, row_idx - 12), -1):
                    if back_row < 1:
                        break
                    prev = values[back_row - 1]
                    segment = prev[left_bound - 1 : right_bound]
                    non_empty = [cell.strip() for cell in segment if cell.strip()]
                    full_non_empty = [cell.strip() for cell in prev if cell.strip()]
                    if not non_empty and not full_non_empty:
                        continue

                    for raw_cell in segment:
                        if "сторона" in normalize_text(raw_cell):
                            side_label = raw_cell.strip()
                            break

                    # Rotation/operation headers are often centered in merged cells outside table columns.
                    joined = " | ".join(non_empty)
                    joined_norm = normalize_text(joined)
                    full_joined = " | ".join(full_non_empty)
                    full_joined_norm = normalize_text(full_joined)
                    if not rotation_label and "ротация" in joined_norm:
                        rotation_label = joined
                    if not rotation_label and "ротация" in full_joined_norm:
                        rotation_label = full_joined
                    if not operation_label and "операция" in joined_norm:
                        operation_label = joined
                    if not operation_label and "операция" in full_joined_norm:
                        operation_label = full_joined

                    if side_label != "Сторона" and rotation_label and operation_label:
                        break

                # Strict filter: only tables with an explicitly detected Rotation 1 header are allowed.
                if not rotation_label:
                    continue
                if not is_rotation_one_text(rotation_label):
                    continue

                event_label = rotation_label or operation_label

                tables.append(
                    {
                        "header_row": row_idx,
                        "squad_no_col": col_idx,
                        "vehicle_col": int(vehicle_col),
                        "slots_col": int(slots_col),
                        "squad_col": int(squad_col),
                        "occupied_col": int(occupied_col),
                        "side": side_label,
                        "event": event_label,
                        "operation": operation_label,
                    }
                )

        return tables

    def _extract_assignment_entries(self, values: list[list[str]]) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for table in self._find_assignment_tables(values):
            header_row = int(table["header_row"])
            squad_no_col = int(table["squad_no_col"])
            vehicle_col = int(table["vehicle_col"])
            slots_col = int(table["slots_col"])
            squad_col = int(table["squad_col"])
            occupied_col = int(table["occupied_col"])
            side = str(table["side"])
            event = str(table["event"])

            empty_streak = 0
            for row_idx in range(header_row + 1, len(values) + 1):
                row = values[row_idx - 1]
                cells = [
                    row[squad_no_col - 1].strip() if squad_no_col - 1 < len(row) else "",
                    row[vehicle_col - 1].strip() if vehicle_col - 1 < len(row) else "",
                    row[slots_col - 1].strip() if slots_col - 1 < len(row) else "",
                    row[squad_col - 1].strip() if squad_col - 1 < len(row) else "",
                    row[occupied_col - 1].strip() if occupied_col - 1 < len(row) else "",
                ]

                joined_norm = normalize_text(" | ".join(cell for cell in cells if cell))
                if "всего слотов" in joined_norm:
                    break

                if not any(cells):
                    empty_streak += 1
                    if empty_streak >= 2:
                        break
                    continue
                empty_streak = 0

                squad_no, vehicle, slots, squad_tag, occupied = cells
                if not squad_no:
                    continue

                entries.append(
                    {
                        "row": row_idx,
                        "event": event,
                        "side": side,
                        "squad_no": squad_no,
                        "vehicle": vehicle,
                        "slots": slots,
                        "squad_tag": squad_tag,
                        "occupied": occupied,
                        "squad_col": squad_col,
                        "occupied_col": occupied_col,
                    }
                )

        return entries

    def _render_assignments_preview(self, values: list[list[str]]) -> Optional[str]:
        entries = self._extract_assignment_entries(values)
        if not entries:
            return None
        messages = self._render_assignments_preview_messages(values)
        return messages[0] if messages else None

    def _render_assignments_preview_messages(self, values: list[list[str]]) -> list[str]:
        entries = self._extract_assignment_entries(values)
        if not entries:
            return []

        shown_entries = entries[: self.preview_max_rows]
        grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
        order: list[tuple[str, str]] = []
        for entry in shown_entries:
            event = str(entry["event"]).strip() or "Ротация 1"
            side = str(entry["side"]).strip() or "Сторона"
            key = (event, side)
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(entry)

        messages: list[str] = []
        for event, side in order:
            lines: list[str] = [
                f"## Отряды и слоты: `{self.worksheet.title}`",
                f"**{event}**",
                f"`{side}`",
                "",
            ]

            for entry in grouped[(event, side)]:
                vehicle_text = truncate_cell(str(entry["vehicle"]), 72) or "-"
                squad_tag_text = truncate_cell(str(entry["squad_tag"]), 20) or "-"
                occupied_text = truncate_cell(str(entry["occupied"]), 8) or "-"
                slots_text = truncate_cell(str(entry["slots"]), 8) or "-"
                squad_no_text = truncate_cell(str(entry["squad_no"]), 26) or "-"
                lines.append(
                    f"Строка `{entry['row']}` | `{squad_no_text}` | Техника `{vehicle_text}`"
                )
                lines.append(
                    f"Слоты `{slots_text}` | Отряд `{squad_tag_text}` | Занято `{occupied_text}`"
                )
                lines.append("")

            while lines and lines[-1] == "":
                lines.pop()
            messages.append("\n".join(lines))

        if len(entries) > len(shown_entries):
            messages.append(
                f"Показано {len(shown_entries)} из {len(entries)} строк. "
                "Увеличь `TABLE_PREVIEW_MAX_ROWS`, если нужно больше."
            )

        return messages



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
            embed = build_reminder_embed(title_text, description, event_weekday, event_time)
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
                reminder_votes.ensure_message(sent_message.id)
                event_at_msk = resolve_next_event_datetime(now_msk, event_weekday, event_time)
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
                    reminder_votes.ensure_message(sent_message.id)
                    event_at_msk = resolve_next_event_datetime(now_msk, event_weekday, event_time)
                    reminder_dispatches.register(sent_message.id, int(item.get("id", 0)), event_at_msk)
                except Exception:
                    pass
            finally:
                reminders.mark_triggered(int(item.get("id", 0)), now_msk.strftime("%Y-%m-%d %H:%M"))

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
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
sheets_writer: Optional[SheetsWriter] = None
commands_synced = False
reminders = ReminderManager(REMINDERS_FILE)
reminder_votes = ReminderVoteStore(REMINDER_VOTES_FILE)
reminder_dispatches = ReminderDispatchStore(REMINDER_DISPATCHES_FILE)
reminder_task: Optional[asyncio.Task] = None
vote_view_registered = False


def get_sheets_writer() -> SheetsWriter:
    global sheets_writer
    if sheets_writer is None:
        sheets_writer = SheetsWriter()
    return sheets_writer


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
        vote_view_registered = True

    now_msk = datetime.now(MSK_TZ)
    print(f"Logged in as {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")
    print(f"Current MSK time: {now_msk:%Y-%m-%d %H:%M:%S}")

    if reminder_task is None or reminder_task.done():
        reminder_task = asyncio.create_task(reminder_worker())


@bot.tree.command(name="sheet", description="Show a preview of the configured Google Sheet worksheet")
async def sheet(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)

    try:
        writer = get_sheets_writer()
        previews = writer.render_preview_messages()
        for preview in previews:
            await send_interaction_text_chunks(interaction, preview)
        await interaction.followup.send(
            "Для записи используй `/occupy row:<номер_строки> count:<число>`.\n"
            "Если в строке две стороны, добавь `side` (например `1` или `2`): `/occupy row:<номер> count:<число> side:1`.\n"
            "Бот в выбранной строке поставит `Отряд = LCN` и `Занято = <число>`."
        )
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Failed to read Google Sheets: {exc}")


@bot.tree.command(
    name="sheet_unselected",
    description="Show only rows where squad is 'Не выбран' (grouped by side) in one message",
)
async def sheet_unselected(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)

    try:
        writer = get_sheets_writer()
        messages = writer.render_unselected_preview_messages()
        for message in messages:
            await send_interaction_text_chunks(interaction, message)
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Failed to read Google Sheets: {exc}")


@bot.tree.command(name="occupy", description="Set squad to LCN and write participant count in a selected row")
@app_commands.describe(
    count="Number of participants to write into the 'Занято' column",
    row="Row number from /sheet preview",
    side="Optional side for rows that contain two tables (examples: 1, 2, 'Сторона 1')",
)
async def occupy(
    interaction: discord.Interaction,
    count: int,
    row: int,
    side: Optional[str] = None,
) -> None:
    await interaction.response.defer(thinking=True)

    try:
        if count < 0:
            await interaction.followup.send("Count must be >= 0.")
            return
        if row < 1:
            await interaction.followup.send("Row must be >= 1.")
            return

        writer = get_sheets_writer()
        squad_tag, squad_col, occupied_col, side_label = writer.write_assignment_for_row(
            row=row,
            count=count,
            side=side,
        )
        await interaction.followup.send(
            f"Updated row {row} ({side_label}): `Отряд` = `{squad_tag}` "
            f"(column {column_index_to_label(squad_col)} / {squad_col}), "
            f"`Занято` = `{count}` (column {column_index_to_label(occupied_col)} / {occupied_col})."
        )
    except ValueError as exc:
        await interaction.followup.send(f"Failed to update row: {exc}")
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Failed to update row: {exc}")



@bot.tree.command(name="add_reminder", description="Add weekly reminder by Moscow time")
@app_commands.describe(
    weekday="Event weekday (1-7, ru/en name; e.g. 5 or пятница)",
    time="Event start time (MSK) HH:MM",
    remind_weekday="When to publish reminder: weekday (optional, default = event weekday)",
    remind_time="When to publish reminder: time HH:MM (optional, default = event time)",
    ping_role="Server role to ping when reminder is posted (optional)",
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
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    await interaction.response.defer(thinking=True)

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
        )
        ping_info = f"\nPing role: `{ping_role.name}`" if ping_role else ""
        await interaction.followup.send(
            f"Reminder added: id `{reminder['id']}`.\n"
            f"Event: `{weekday_label(weekday_index)} {event_hhmm}` (MSK)\n"
            f"Publish: `{weekday_label(publish_weekday_index)} {publish_hhmm}` (MSK), channel `{interaction.channel_id}`."
            f"{ping_info}"
        )
    except ValueError as exc:
        await interaction.followup.send(f"Failed to add reminder: {exc}")
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(f"Failed to add reminder: {exc}")


@bot.tree.command(name="list_reminders", description="List configured reminders (Moscow time)")
async def list_reminders(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)

    items = reminders.all()
    if not items:
        await interaction.followup.send("No reminders configured.")
        return

    lines = ["## Reminders (MSK)"]
    for item in items:
        event_weekday = int(item.get("weekday", -1))
        event_time = str(item.get("time", ""))
        publish_weekday = int(item.get("remind_weekday", event_weekday))
        publish_time = str(item.get("remind_time", event_time))
        lines.append(
            f"id `{item.get('id')}` | event `{weekday_label(event_weekday)} {event_time}` | channel `{item.get('channel_id')}`"
        )
        lines.append(f"publish: {weekday_label(publish_weekday)} {publish_time}")
        role_id = int(item.get("role_id", 0) or 0)
        lines.append(f"ping_role: {f'<@&{role_id}>' if role_id > 0 else '-'}")
        title_text = str(item.get("title", "")).strip() or str(item.get("message", "")).strip()
        lines.append(f"title: {truncate_cell(title_text, 120)}")
        lines.append(f"description: {truncate_cell(str(item.get('description', '')), 120) or '-'}")

    await send_interaction_text_chunks(interaction, "\n".join(lines))


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






