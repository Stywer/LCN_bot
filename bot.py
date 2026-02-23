import os
from typing import Optional
import re

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
                    f"row `{entry['row']}` | `{squad_no_text}` | Техника `{vehicle_text}`"
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


load_dotenv(encoding="utf-8-sig")

TOKEN = require_env("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
sheets_writer: Optional[SheetsWriter] = None
commands_synced = False


def get_sheets_writer() -> SheetsWriter:
    global sheets_writer
    if sheets_writer is None:
        sheets_writer = SheetsWriter()
    return sheets_writer


@bot.event
async def on_ready() -> None:
    global commands_synced

    if not commands_synced:
        await bot.tree.sync()
        commands_synced = True

    print(f"Logged in as {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")


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


@bot.tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("pong")


if __name__ == "__main__":
    bot.run(TOKEN)
