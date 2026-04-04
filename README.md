# LCN Bot

Discord-бот для клана LCN: работа с таблицей слотов Arma в Google Sheets, запись отряда и система напоминаний об ивентах.

## Функционал

- `/sheet`  
  Показывает найденные таблицы отрядов/слотов (только `Ротация 1`).

- `/sheet_unselected`  
  Показывает только строки, где `Отряд = Не выбран`.

- `/occupy row:<номер> count:<число> [side:<сторона>]`  
  В выбранной строке записывает:
  - `Отряд = LCN` (или значение `CLAN_NAME`)
  - `Занято = <count>`
  Если в строке есть две стороны, укажи `side`.

- `/add_reminder`  
  Добавляет еженедельное напоминание:
  - `weekday` — день события (1-7, ru/en)
  - `time` — время начала события (МСК)
  - `remind_weekday` — день публикации напоминания (опционально)
  - `remind_time` — время публикации напоминания (опционально)
  - `ping_role` — роль сервера для пинга (опционально)
  - `title` — заголовок
  - `description` — описание

- `/list_reminders`  
  Показывает список напоминаний:
  - событие (`weekday/time`)
  - публикация (`remind_weekday/remind_time`)
  - канал
  - роль для пинга
  - title/description

- `/delete_reminder reminder_id:<id>`  
  Удаляет напоминание по ID.

- `/delete_reminder_list`  
  Интерактивное удаление напоминания из выпадающего списка.

- `/ping`  
  Проверка, что бот онлайн.

## Напоминания и голосование

При публикации напоминания бот отправляет embed + кнопки:
- `✅ Приду`
- `❌ Не приду`

Бот показывает, кто и как проголосовал прямо в сообщении.

Дополнительно:
- за 30 минут до начала события бот отправляет ЛС всем, кто выбрал `✅ Приду`.

## Файлы состояния

- `reminders.json` — список напоминаний
- `reminder_votes.json` — голоса по сообщениям
- `reminder_dispatches.json` — служебное состояние DM-рассылки за 30 минут

## Требования

- Python 3.11+
- зависимости из `requirements.txt`

## Установка (локально)

1. Установи зависимости:
   - `pip install -r requirements.txt`
2. Заполни `.env`
3. Положи `service_account.json` в корень проекта
4. Запусти:
   - `python bot.py`

## Переменные `.env`

Обязательные:
- `DISCORD_BOT_TOKEN`
- `CLAN_NAME`
- `GOOGLE_SERVICE_ACCOUNT_FILE` (обычно `service_account.json`)
- `GOOGLE_SHEET_ID`
- `GOOGLE_WORKSHEET_NAME`

Опциональные:
- `DISCORD_GUILD_ID` (ускоренная синхронизация slash-команд в конкретной гильдии)
- `START_ROW` (по умолчанию `2`)
- `TABLE_PREVIEW_MAX_ROWS`
- `TABLE_PREVIEW_MAX_COLS`
- `TARGET_COLUMN` (необязательно)

## Обновление на сервере

```bash
cd /opt/LCN_bot
git checkout dev
git pull origin dev
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart lcn-bot
```

## Примечания

- Если slash-команды не обновились сразу, подожди немного и открой меню `/` заново.
- Для корректного чтения Google Sheets сервисный аккаунт должен иметь доступ к таблице.
