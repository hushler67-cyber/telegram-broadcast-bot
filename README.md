# Telegram Broadcast Bot

Save broadcast messages, track groups where the bot is admin, send now or on a repeating schedule — all from the bot's DM with buttons.

## Files
- `bot.py` — the bot
- `requirements.txt` — dependencies
- `Procfile` — tells Railway to run `python bot.py` as a worker

## Deploy on Railway
1. Push these files to a GitHub repo and create a new Railway project from it.
2. In **Variables**, add:
   - `BOT_TOKEN` — from @BotFather (required)
   - `GROUP_ID` — e.g. `-1001234567890` (optional; the bot must be admin there)
   - `ADMIN_IDS` — your Telegram user ID, comma-separated for more (optional but recommended; if unset, the first person to /start becomes owner)
3. **Keep data across redeploys:** add a Volume, mount it at `/data`, and set `DATA_DIR=/data`. Without a volume, saved broadcasts/groups/schedules reset on every deploy.
4. Deploy. Logs should show `Bot starting…`.

## Using it
- DM the bot `/start` → menu.
- **Add a group:** add the bot to the group and promote it to admin — it's saved automatically (or use Groups → Add by ID).
- **➕ New Broadcast:** send any text/photo/video/GIF/document; formatting is kept.
- **📤 Send Now:** pick broadcast → pick group (or All groups).
- **⏰ Schedule:** pick broadcast → group → interval (presets or custom like `45m`, `2h`, `1h30m`). First post goes out immediately.
- **📋 Active Schedules:** see and stop schedules.
- `/cancel` stops any pending input.

Removing the bot from a group (or demoting it) removes that group and stops its schedules automatically.
