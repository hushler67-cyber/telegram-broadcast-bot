"""
Telegram Broadcast Bot
- Save broadcast messages (text, photo, video, GIF, document — formatting kept)
- Edit or rename saved broadcasts (buttons and running schedules are kept)
- Attach link buttons to any broadcast (several per row, several rows)
- Auto-tracks groups/channels where the bot is made admin
- Send a saved broadcast to a chosen group (or all groups) on demand
- Schedule a broadcast to repeat every N minutes / hours
- Everything controlled from the bot's DM with inline buttons

Env vars:
  BOT_TOKEN   (required)  Telegram bot token from @BotFather
  GROUP_ID    (optional)  A group ID to register on startup (bot must be admin there)
  ADMIN_IDS   (optional)  Comma-separated Telegram user IDs allowed to control the bot.
                          If not set, the FIRST person to /start the bot becomes the owner.
  DATA_DIR    (optional)  Folder for data.json. On Railway, mount a volume (e.g. /data)
                          and set DATA_DIR=/data so data survives redeploys.
"""

import html
import json
import logging
import os
import re
import uuid

from telegram import (
    BotCommand,
    ChatMember,
    InlineKeyboardButton as B,
    InlineKeyboardMarkup as M,
    MessageEntity,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ───────────────────────── Config ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = os.getenv("GROUP_ID", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DATA_DIR = os.getenv("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "data.json")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("broadcast-bot")

INTERVAL_PRESETS = [
    ("1 min", 60), ("5 min", 300), ("10 min", 600), ("15 min", 900),
    ("30 min", 1800), ("1 hr", 3600), ("2 hrs", 7200), ("3 hrs", 10800),
    ("6 hrs", 21600), ("12 hrs", 43200), ("24 hrs", 86400),
]

BUTTONS_HELP = (
    "🔘 <b>Add buttons</b>\n\n"
    "Send one row per line, in the format <code>Button text - link</code>.\n"
    "To put several buttons on the same row, separate them with <code>|</code>.\n\n"
    "<b>Example:</b>\n"
    "<code>Join Channel - https://t.me/yourchannel\n"
    "Website - https://example.com | Contact - https://t.me/yourname</code>\n\n"
    "That makes 1 button on the first row and 2 on the second.\n"
    "Links must start with https://, http:// or t.me/"
)


# ───────────────────────── Storage ─────────────────────────
class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.data = {"owner": None, "groups": {}, "broadcasts": {}, "schedules": {}}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception as e:
                log.error("Could not read %s: %s", path, e)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @property
    def groups(self) -> dict:
        return self.data["groups"]

    @property
    def broadcasts(self) -> dict:
        return self.data["broadcasts"]

    @property
    def schedules(self) -> dict:
        return self.data["schedules"]


store = Store(DATA_FILE)


# ───────────────────────── Helpers ─────────────────────────
def short_id() -> str:
    return uuid.uuid4().hex[:8]


def esc(s) -> str:
    return html.escape(str(s or ""))


def fmt_interval(secs: int) -> str:
    h, rem = divmod(int(secs), 3600)
    m = rem // 60
    parts = []
    if h:
        parts.append(f"{h} hr" + ("s" if h > 1 else ""))
    if m:
        parts.append(f"{m} min")
    return "every " + " ".join(parts)


def parse_interval(text: str):
    """Accepts: 30, 30m, 30 min, 2h, 2 hours, 1h30m, 1h 30min. Returns seconds or None."""
    t = (text or "").strip().lower()
    if t.isdigit():
        return int(t) * 60 if int(t) > 0 else None
    m = re.fullmatch(
        r"(?:(\d+)\s*h(?:ours?|rs?|r)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?|ins)?)?", t
    )
    if not m or not (m.group(1) or m.group(2)):
        return None
    secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    return secs if secs >= 60 else None


def parse_buttons(text: str):
    """
    Parse button layout text.
      One row per line; buttons in a row separated by '|'; each button is 'Text - URL'.
    Returns (rows, error). rows = [[[text, url], ...], ...]
    """
    rows = []
    for line_no, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = []
        for chunk in line.split("|"):
            chunk = chunk.strip()
            if not chunk:
                continue
            m = re.match(r"^(.+?)\s+[-–—]\s+(\S+)$", chunk)
            if not m:
                return None, f"Line {line_no}: “{chunk}” — use the format  Text - link"
            label, url = m.group(1).strip(), m.group(2).strip()
            if url.lower().startswith(("t.me/", "telegram.me/")):
                url = "https://" + url
            if not re.match(r"^(https?://|tg://)\S+\.?\S*$", url, re.I):
                return None, f"Line {line_no}: “{url}” isn't a valid link (start it with https://)"
            if len(label) > 64:
                return None, f"Line {line_no}: button text is too long (max 64 characters)"
            row.append([label, url])
        if row:
            if len(row) > 8:
                return None, f"Line {line_no}: max 8 buttons per row"
            rows.append(row)
    if not rows:
        return None, "I didn't find any buttons. Use the format  Text - link"
    if sum(len(r) for r in rows) > 50:
        return None, "Too many buttons (max 50)."
    return rows, None


def buttons_markup(bc: dict):
    rows = bc.get("buttons") or []
    if not rows:
        return None
    return M([[B(label, url=url) for label, url in row] for row in rows])


def buttons_summary(bc: dict) -> str:
    rows = bc.get("buttons") or []
    if not rows:
        return "No buttons"
    n = sum(len(r) for r in rows)
    return f"{n} button{'s' if n != 1 else ''} in {len(rows)} row{'s' if len(rows) != 1 else ''}"


def group_title(gid: str) -> str:
    if gid == "all":
        return "All groups"
    return store.groups.get(str(gid), {}).get("title", str(gid))


def is_authorized(user_id: int) -> bool:
    if ADMIN_IDS:
        return user_id in ADMIN_IDS
    return store.data.get("owner") == user_id


def extract_broadcast(msg) -> dict | None:
    """Turn a DM message into a storable broadcast (keeps formatting via entities)."""
    ents = [e.to_dict() for e in (msg.entities or msg.caption_entities or [])]
    if msg.text:
        return {"type": "text", "text": msg.text, "entities": ents, "buttons": []}
    for kind in ("photo", "video", "animation", "document"):
        obj = getattr(msg, kind, None)
        if obj:
            file_id = obj[-1].file_id if kind == "photo" else obj.file_id
            return {"type": kind, "file_id": file_id, "text": msg.caption, "entities": ents, "buttons": []}
    return None


async def send_broadcast(bot, chat_id: int, bc: dict):
    ents = [MessageEntity.de_json(e, bot) for e in bc.get("entities", [])] or None
    kind, text = bc["type"], bc.get("text")
    kb = buttons_markup(bc)
    if kind == "text":
        await bot.send_message(chat_id, text, entities=ents, reply_markup=kb)
    elif kind == "photo":
        await bot.send_photo(chat_id, bc["file_id"], caption=text, caption_entities=ents, reply_markup=kb)
    elif kind == "video":
        await bot.send_video(chat_id, bc["file_id"], caption=text, caption_entities=ents, reply_markup=kb)
    elif kind == "animation":
        await bot.send_animation(chat_id, bc["file_id"], caption=text, caption_entities=ents, reply_markup=kb)
    elif kind == "document":
        await bot.send_document(chat_id, bc["file_id"], caption=text, caption_entities=ents, reply_markup=kb)


async def send_to_targets(bot, bc: dict, gid: str) -> tuple[list, list]:
    targets = list(store.groups.keys()) if gid == "all" else [gid]
    ok, failed = [], []
    for g in targets:
        try:
            await send_broadcast(bot, int(g), bc)
            ok.append(g)
        except TelegramError as e:
            log.warning("Send to %s failed: %s", g, e)
            failed.append((g, str(e)))
    return ok, failed


# ───────────────────────── Scheduler ─────────────────────────
async def run_schedule(ctx: ContextTypes.DEFAULT_TYPE):
    sid = ctx.job.data
    s = store.schedules.get(sid)
    if not s:
        ctx.job.schedule_removal()
        return
    bc = store.broadcasts.get(s["bid"])
    if not bc or (s["gid"] != "all" and s["gid"] not in store.groups):
        log.info("Schedule %s removed (broadcast or group no longer exists)", sid)
        store.schedules.pop(sid, None)
        store.save()
        ctx.job.schedule_removal()
        return
    ok, failed = await send_to_targets(ctx.bot, bc, s["gid"])
    log.info("Schedule %s → sent %d, failed %d", sid, len(ok), len(failed))


def start_job(app: Application, sid: str, secs: int, first: int):
    app.job_queue.run_repeating(run_schedule, interval=secs, first=first, name=sid, data=sid)


def stop_job(app: Application, sid: str):
    for job in app.job_queue.get_jobs_by_name(sid):
        job.schedule_removal()


def remove_schedules_where(app: Application, key: str, value: str) -> int:
    sids = [sid for sid, s in store.schedules.items() if s[key] == value]
    for sid in sids:
        stop_job(app, sid)
        store.schedules.pop(sid, None)
    return len(sids)


# ───────────────────────── Keyboards ─────────────────────────
def main_menu_kb():
    return M([
        [B("➕ New Broadcast", callback_data="new_bc"), B("📚 Broadcasts", callback_data="list_bc")],
        [B("📤 Send Now", callback_data="send"), B("⏰ Schedule", callback_data="sched")],
        [B("👥 Groups", callback_data="groups"), B("📋 Active Schedules", callback_data="schedules")],
    ])


def back_kb(target="menu"):
    return M([[B("⬅️ Back", callback_data=target)]])


def cancel_kb(target="menu"):
    return M([[B("✖️ Cancel", callback_data=target)]])


def manage_bc_kb(bid: str):
    bc = store.broadcasts.get(bid, {})
    has_btns = bool(bc.get("buttons"))
    rows = [
        [B("📤 Send now", callback_data=f"s:{bid}"), B("⏰ Schedule", callback_data=f"sc:{bid}")],
        [B("✏️ Edit message", callback_data=f"em:{bid}"), B("🏷 Rename", callback_data=f"rn:{bid}")],
        [B("🔘 Edit buttons" if has_btns else "🔘 Add buttons", callback_data=f"bb:{bid}")],
    ]
    if has_btns:
        rows.append([B("🧹 Remove buttons", callback_data=f"bx:{bid}")])
    rows.append([B("🗑 Delete broadcast", callback_data=f"d:{bid}")])
    rows.append([B("⬅️ Back", callback_data="list_bc")])
    return M(rows)


def pick_bc_kb(prefix: str):
    rows = [[B(f"📄 {bc['name']}", callback_data=f"{prefix}:{bid}")] for bid, bc in store.broadcasts.items()]
    rows.append([B("⬅️ Back", callback_data="menu")])
    return M(rows)


def pick_group_kb(prefix: str, bid: str, back: str):
    rows = [[B(f"👥 {g['title']}", callback_data=f"{prefix}:{bid}:{gid}")] for gid, g in store.groups.items()]
    if len(store.groups) > 1:
        rows.append([B("🌐 All groups", callback_data=f"{prefix}:{bid}:all")])
    rows.append([B("⬅️ Back", callback_data=back)])
    return M(rows)


def interval_kb(bid: str, gid: str):
    btns = [B(label, callback_data=f"si:{bid}:{gid}:{secs}") for label, secs in INTERVAL_PRESETS]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([B("✏️ Custom interval", callback_data=f"sx:{bid}:{gid}")])
    rows.append([B("⬅️ Back", callback_data=f"sc:{bid}")])
    return M(rows)


# ───────────────────────── UI helpers ─────────────────────────
async def show(update: Update, text: str, kb: M):
    """Edit the menu message if coming from a button, otherwise send a new one."""
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    await update.effective_chat.send_message(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def show_main(update: Update):
    await show(
        update,
        "📣 <b>Broadcast Bot</b>\n\n"
        f"Broadcasts saved: <b>{len(store.broadcasts)}</b>\n"
        f"Groups (admin): <b>{len(store.groups)}</b>\n"
        f"Active schedules: <b>{len(store.schedules)}</b>\n\n"
        "Choose an option:",
        main_menu_kb(),
    )


async def preview_and_manage(chat, bot, bid: str, header: str = "👇 Preview:"):
    """Send a live preview of the broadcast (with its buttons) followed by its manage menu."""
    bc = store.broadcasts[bid]
    await chat.send_message(header)
    try:
        await send_broadcast(bot, chat.id, bc)
    except TelegramError as e:
        await chat.send_message(f"⚠️ Preview failed: {e}")
    await chat.send_message(
        f"📄 <b>{esc(bc['name'])}</b>\n🔘 {buttons_summary(bc)}",
        parse_mode=ParseMode.HTML,
        reply_markup=manage_bc_kb(bid),
    )


async def guard(update: Update) -> bool:
    chat = update.effective_chat
    if not chat or chat.type != ChatType.PRIVATE:
        return False
    if is_authorized(update.effective_user.id):
        return True
    if update.callback_query:
        await update.callback_query.answer("⛔ Not authorized", show_alert=True)
    else:
        await update.effective_message.reply_text("⛔ You're not authorized to use this bot.")
    return False


# ───────────────────────── Commands ─────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    if not ADMIN_IDS and store.data.get("owner") is None:
        store.data["owner"] = uid
        store.save()
        await update.message.reply_text("✅ You're now the owner of this bot.")
    if not await guard(update):
        return
    ctx.user_data.pop("awaiting", None)
    await show_main(update)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    ctx.user_data.pop("awaiting", None)
    await update.message.reply_text("Cancelled.")
    await show_main(update)


# ───────────────────────── Buttons ─────────────────────────
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    q = update.callback_query
    await q.answer()
    ctx.user_data.pop("awaiting", None)  # any button press cancels pending input
    parts = q.data.split(":")
    cmd = parts[0]
    app = ctx.application

    # ── Main menu
    if cmd == "menu":
        await show_main(update)

    # ── Broadcasts
    elif cmd == "new_bc":
        ctx.user_data["awaiting"] = {"kind": "bc_msg"}
        await show(
            update,
            "📝 <b>New broadcast</b>\n\nSend me the message exactly as you want it posted.\n"
            "Supported: text, photo, video, GIF, document (with caption). Bold, italic and links are kept.\n\n"
            "You can add link buttons right after saving.",
            cancel_kb(),
        )

    elif cmd == "list_bc":
        if not store.broadcasts:
            await show(update, "📚 No broadcasts saved yet.",
                       M([[B("➕ New Broadcast", callback_data="new_bc")], [B("⬅️ Back", callback_data="menu")]]))
        else:
            await show(update, "📚 <b>Saved broadcasts</b>\nTap one to preview or manage it.", pick_bc_kb("v"))

    elif cmd == "v":
        bid = parts[1]
        if bid not in store.broadcasts:
            return await show(update, "That broadcast no longer exists.", back_kb("list_bc"))
        await preview_and_manage(update.effective_chat, ctx.bot, bid)

    elif cmd == "d":
        bid = parts[1]
        bc = store.broadcasts.pop(bid, None)
        n = remove_schedules_where(app, "bid", bid)
        store.save()
        msg = f"🗑 Deleted <b>{esc(bc['name'])}</b>" if bc else "Already deleted."
        if n:
            msg += f"\n⏹ Stopped {n} schedule(s) using it."
        await show(update, msg, back_kb("list_bc"))

    # ── Edit a saved broadcast
    elif cmd == "em":
        bid = parts[1]
        bc = store.broadcasts.get(bid)
        if not bc:
            return await show(update, "That broadcast no longer exists.", back_kb("list_bc"))
        ctx.user_data["awaiting"] = {"kind": "edit_msg", "bid": bid}
        await show(
            update,
            f"✏️ <b>Edit “{esc(bc['name'])}”</b>\n\n"
            "Send the new version of the message — text, photo, video, GIF or document.\n"
            "It replaces the old content. Its buttons and any running schedules are kept.\n\n"
            "Tip: to change only the text, copy the preview above, edit it, and send it back.",
            cancel_kb(f"v:{bid}"),
        )

    elif cmd == "rn":
        bid = parts[1]
        bc = store.broadcasts.get(bid)
        if not bc:
            return await show(update, "That broadcast no longer exists.", back_kb("list_bc"))
        ctx.user_data["awaiting"] = {"kind": "rename", "bid": bid}
        await show(
            update,
            f"🏷 Current name: <b>{esc(bc['name'])}</b>\n\nSend the new name (max 40 characters).\n"
            "This is only the label in your menu — it isn't posted to groups.",
            cancel_kb(f"v:{bid}"),
        )

    # ── Link buttons on a broadcast
    elif cmd == "bb":
        bid = parts[1]
        bc = store.broadcasts.get(bid)
        if not bc:
            return await show(update, "That broadcast no longer exists.", back_kb("list_bc"))
        ctx.user_data["awaiting"] = {"kind": "buttons", "bid": bid}
        text = BUTTONS_HELP
        if bc.get("buttons"):
            current = "\n".join(" | ".join(f"{t} - {u}" for t, u in row) for row in bc["buttons"])
            text += f"\n\n<b>Current buttons</b> (sending new ones replaces them):\n<code>{esc(current)}</code>"
        await show(update, text, cancel_kb(f"v:{bid}"))

    elif cmd == "bx":
        bid = parts[1]
        bc = store.broadcasts.get(bid)
        if not bc:
            return await show(update, "That broadcast no longer exists.", back_kb("list_bc"))
        bc["buttons"] = []
        store.save()
        await show(update, f"🧹 Removed buttons from <b>{esc(bc['name'])}</b>.", manage_bc_kb(bid))

    # ── Groups
    elif cmd == "groups":
        if store.groups:
            lines = [f"• <b>{esc(g['title'])}</b>\n  <code>{gid}</code>" for gid, g in store.groups.items()]
            text = "👥 <b>Groups where I'm admin</b>\n\n" + "\n".join(lines)
        else:
            text = ("👥 No groups yet.\n\nAdd me to a group and promote me to <b>admin</b> — "
                    "I'll save it automatically. Or add one by ID.")
        rows = [[B(f"❌ Remove {g['title']}", callback_data=f"rg:{gid}")] for gid, g in store.groups.items()]
        rows.append([B("➕ Add group by ID", callback_data="add_g")])
        rows.append([B("⬅️ Back", callback_data="menu")])
        await show(update, text, M(rows))

    elif cmd == "add_g":
        ctx.user_data["awaiting"] = {"kind": "group_id"}
        await show(update, "Send the group ID (e.g. <code>-1001234567890</code>).\nI must already be an admin there.",
                   cancel_kb())

    elif cmd == "rg":
        gid = parts[1]
        g = store.groups.pop(gid, None)
        n = remove_schedules_where(app, "gid", gid)
        store.save()
        msg = f"Removed <b>{esc(g['title'])}</b>." if g else "Already removed."
        if n:
            msg += f"\n⏹ Stopped {n} schedule(s) for it."
        await show(update, msg, back_kb("groups"))

    # ── Send now
    elif cmd == "send":
        if not store.broadcasts:
            return await show(update, "No broadcasts saved yet.", back_kb())
        await show(update, "📤 <b>Send now</b>\nWhich broadcast?", pick_bc_kb("s"))

    elif cmd == "s":
        bid = parts[1]
        if bid not in store.broadcasts:
            return await show(update, "That broadcast no longer exists.", back_kb())
        if not store.groups:
            return await show(update, "No groups yet. Make me admin in a group first.", back_kb())
        await show(update, f"📤 Send <b>{esc(store.broadcasts[bid]['name'])}</b> to which group?",
                   pick_group_kb("sg", bid, "send"))

    elif cmd == "sg":
        bid, gid = parts[1], parts[2]
        bc = store.broadcasts.get(bid)
        if not bc:
            return await show(update, "That broadcast no longer exists.", back_kb())
        ok, failed = await send_to_targets(ctx.bot, bc, gid)
        text = f"✅ Sent to {len(ok)} group(s)."
        for g, err in failed:
            text += f"\n⚠️ {esc(group_title(g))}: {esc(err)}"
        await show(update, text, back_kb())

    # ── Schedule
    elif cmd == "sched":
        if not store.broadcasts:
            return await show(update, "No broadcasts saved yet.", back_kb())
        await show(update, "⏰ <b>Schedule</b>\nWhich broadcast should repeat?", pick_bc_kb("sc"))

    elif cmd == "sc":
        bid = parts[1]
        if bid not in store.broadcasts:
            return await show(update, "That broadcast no longer exists.", back_kb())
        if not store.groups:
            return await show(update, "No groups yet. Make me admin in a group first.", back_kb())
        await show(update, f"⏰ Schedule <b>{esc(store.broadcasts[bid]['name'])}</b> in which group?",
                   pick_group_kb("scg", bid, "sched"))

    elif cmd == "scg":
        bid, gid = parts[1], parts[2]
        await show(update, f"⏰ How often should it post to <b>{esc(group_title(gid))}</b>?",
                   interval_kb(bid, gid))

    elif cmd == "si":
        bid, gid, secs = parts[1], parts[2], int(parts[3])
        await create_schedule(update, ctx, bid, gid, secs)

    elif cmd == "sx":
        bid, gid = parts[1], parts[2]
        ctx.user_data["awaiting"] = {"kind": "interval", "bid": bid, "gid": gid}
        await show(update, "✏️ Type the interval, e.g. <code>45m</code>, <code>2h</code>, "
                           "<code>1h30m</code>, or just <code>20</code> (minutes). Minimum 1 minute.",
                   cancel_kb())

    elif cmd == "schedules":
        if not store.schedules:
            return await show(update, "📋 No active schedules.", back_kb())
        lines, rows = [], []
        for i, (sid, s) in enumerate(store.schedules.items(), 1):
            bc_name = store.broadcasts.get(s["bid"], {}).get("name", "?")
            lines.append(f"{i}. <b>{esc(bc_name)}</b> → {esc(group_title(s['gid']))}, {fmt_interval(s['seconds'])}")
            rows.append([B(f"⏹ Stop #{i}", callback_data=f"st:{sid}")])
        rows.append([B("⬅️ Back", callback_data="menu")])
        await show(update, "📋 <b>Active schedules</b>\n\n" + "\n".join(lines), M(rows))

    elif cmd == "st":
        sid = parts[1]
        stop_job(app, sid)
        store.schedules.pop(sid, None)
        store.save()
        await show(update, "⏹ Schedule stopped.", back_kb("schedules"))


async def create_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE, bid: str, gid: str, secs: int):
    if bid not in store.broadcasts:
        return await show(update, "That broadcast no longer exists.", back_kb())
    sid = short_id()
    store.schedules[sid] = {"bid": bid, "gid": gid, "seconds": secs}
    store.save()
    start_job(ctx.application, sid, secs, first=1)  # first post goes out right away
    await show(
        update,
        f"✅ Scheduled <b>{esc(store.broadcasts[bid]['name'])}</b> → <b>{esc(group_title(gid))}</b>, "
        f"{fmt_interval(secs)}.\nThe first post is going out now.",
        M([[B("📋 Active Schedules", callback_data="schedules")], [B("⬅️ Menu", callback_data="menu")]]),
    )


# ───────────────────────── DM text/media input ─────────────────────────
async def on_private_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    msg = update.message
    aw = ctx.user_data.get("awaiting")
    if not aw:
        return await show_main(update)

    kind = aw["kind"]

    if kind == "bc_msg":
        bc = extract_broadcast(msg)
        if not bc:
            return await msg.reply_text("Unsupported message type. Send text, photo, video, GIF or a document.")
        preview = (bc.get("text") or "").strip().replace("\n", " ")
        bc["name"] = (preview[:28] + "…") if len(preview) > 28 else (preview or f"{bc['type'].title()} #{len(store.broadcasts) + 1}")
        bid = short_id()
        store.broadcasts[bid] = bc
        store.save()
        ctx.user_data.pop("awaiting", None)
        await msg.reply_text(
            f"✅ Saved broadcast <b>{esc(bc['name'])}</b>\n\nWant to add link buttons under it?",
            parse_mode=ParseMode.HTML,
            reply_markup=M([
                [B("🔘 Add buttons", callback_data=f"bb:{bid}")],
                [B("📤 Send now", callback_data=f"s:{bid}"), B("⏰ Schedule", callback_data=f"sc:{bid}")],
                [B("⬅️ Menu", callback_data="menu")],
            ]),
        )

    elif kind == "edit_msg":
        bid = aw["bid"]
        old = store.broadcasts.get(bid)
        if not old:
            ctx.user_data.pop("awaiting", None)
            return await msg.reply_text("That broadcast no longer exists.", reply_markup=back_kb("list_bc"))
        new = extract_broadcast(msg)
        if not new:
            return await msg.reply_text("Unsupported message type. Send text, photo, video, GIF or a document.")
        new["name"] = old["name"]
        new["buttons"] = old.get("buttons", [])
        store.broadcasts[bid] = new
        store.save()
        ctx.user_data.pop("awaiting", None)
        n = sum(1 for s in store.schedules.values() if s["bid"] == bid)
        header = "✅ Broadcast updated."
        if n:
            header += f" {n} running schedule(s) will post the new version."
        await preview_and_manage(update.effective_chat, ctx.bot, bid, header=header + "\n👇 Preview:")

    elif kind == "rename":
        bid = aw["bid"]
        bc = store.broadcasts.get(bid)
        if not bc:
            ctx.user_data.pop("awaiting", None)
            return await msg.reply_text("That broadcast no longer exists.", reply_markup=back_kb("list_bc"))
        name = " ".join((msg.text or "").split())
        if not name:
            return await msg.reply_text("Send the name as text, or /cancel.")
        if len(name) > 40:
            return await msg.reply_text(f"That's {len(name)} characters — keep it to 40 or less.")
        bc["name"] = name
        store.save()
        ctx.user_data.pop("awaiting", None)
        await msg.reply_text(
            f"🏷 Renamed to <b>{esc(name)}</b>\n🔘 {buttons_summary(bc)}",
            parse_mode=ParseMode.HTML,
            reply_markup=manage_bc_kb(bid),
        )

    elif kind == "buttons":
        bid = aw["bid"]
        if bid not in store.broadcasts:
            ctx.user_data.pop("awaiting", None)
            return await msg.reply_text("That broadcast no longer exists.", reply_markup=back_kb("list_bc"))
        rows, err = parse_buttons(msg.text or "")
        if err:
            return await msg.reply_text(f"⚠️ {err}\n\nTry again, or /cancel.")
        store.broadcasts[bid]["buttons"] = rows
        store.save()
        ctx.user_data.pop("awaiting", None)
        await preview_and_manage(update.effective_chat, ctx.bot, bid, header="✅ Buttons saved. Here's how it looks:")

    elif kind == "group_id":
        try:
            gid = int((msg.text or "").strip())
        except ValueError:
            return await msg.reply_text("That's not a valid ID. Send something like -1001234567890 or /cancel.")
        try:
            chat = await ctx.bot.get_chat(gid)
            me = await ctx.bot.get_chat_member(gid, ctx.bot.id)
        except TelegramError as e:
            return await msg.reply_text(f"⚠️ Couldn't access that chat: {e}\nAdd me there first, or /cancel.")
        if me.status != ChatMember.ADMINISTRATOR:
            return await msg.reply_text("⚠️ I'm in that chat but not an admin. Promote me, then try again.")
        store.groups[str(gid)] = {"title": chat.title or str(gid)}
        store.save()
        ctx.user_data.pop("awaiting", None)
        await msg.reply_text(f"✅ Added <b>{esc(chat.title)}</b>.", parse_mode=ParseMode.HTML,
                             reply_markup=back_kb("groups"))

    elif kind == "interval":
        secs = parse_interval(msg.text or "")
        if not secs:
            return await msg.reply_text("Couldn't read that. Try 30m, 2h, 1h30m — or /cancel.")
        ctx.user_data.pop("awaiting", None)
        await create_schedule(update, ctx, aw["bid"], aw["gid"], secs)


# ───────────────────────── Group tracking ─────────────────────────
async def on_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fires when the bot's own status changes in a chat (added, promoted, demoted, removed)."""
    cm = update.my_chat_member
    chat = cm.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        return
    gid = str(chat.id)
    status = cm.new_chat_member.status
    if status == ChatMember.ADMINISTRATOR:
        store.groups[gid] = {"title": chat.title or gid}
        store.save()
        log.info("Now admin in %s (%s)", chat.title, gid)
    elif gid in store.groups:
        store.groups.pop(gid)
        remove_schedules_where(ctx.application, "gid", gid)
        store.save()
        log.info("Lost admin / removed from %s (%s)", chat.title, gid)


async def on_migrate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Group upgraded to supergroup → its ID changes. Move data to the new ID."""
    msg = update.message
    old, new = str(msg.chat.id), str(msg.migrate_to_chat_id)
    if old in store.groups:
        store.groups[new] = store.groups.pop(old)
        for s in store.schedules.values():
            if s["gid"] == old:
                s["gid"] = new
        store.save()
        log.info("Group migrated %s → %s", old, new)


# ───────────────────────── Startup ─────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Open the menu"),
        BotCommand("menu", "Open the menu"),
        BotCommand("cancel", "Cancel current input"),
    ])

    if GROUP_ID:
        try:
            chat = await app.bot.get_chat(int(GROUP_ID))
            me = await app.bot.get_chat_member(int(GROUP_ID), app.bot.id)
            if me.status == ChatMember.ADMINISTRATOR:
                store.groups[str(chat.id)] = {"title": chat.title or GROUP_ID}
                store.save()
                log.info("Registered GROUP_ID %s (%s)", GROUP_ID, chat.title)
            else:
                log.warning("Bot is not admin in GROUP_ID %s", GROUP_ID)
        except TelegramError as e:
            log.warning("Couldn't load GROUP_ID %s: %s", GROUP_ID, e)

    for sid, s in store.schedules.items():
        start_job(app, sid, s["seconds"], first=s["seconds"])
    log.info("Restored %d schedule(s)", len(store.schedules))


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND &
        (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL),
        on_private_message,
    ))

    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
