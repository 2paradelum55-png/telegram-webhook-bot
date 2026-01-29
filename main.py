import os
import re
import time
import sqlite3
from urllib.parse import urlparse
from datetime import datetime, timedelta

import pytz
from flask import Flask, request
from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ================== CONFIG ==================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

TZ = pytz.timezone("Europe/Kyiv")
DB_PATH = os.getenv("DB_PATH", "bot.db")
WEBHOOK_PATH = "/webhook"

URL_RE = re.compile(r"(https?://\S+|t\.me/\S+|www\.\S+)", re.IGNORECASE)

# In-memory flood buckets: {(chat_id, user_id): [timestamps]}
FLOOD_BUCKET = {}

# ================== DB ==================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                anti_links INTEGER DEFAULT 1,
                flood_n INTEGER DEFAULT 6,
                flood_window_sec INTEGER DEFAULT 10,
                flood_mute_min INTEGER DEFAULT 15,
                newbie_protect_min INTEGER DEFAULT 15,
                log_mode TEXT DEFAULT 'here'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS whitelist_domains (
                chat_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                PRIMARY KEY (chat_id, domain)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS join_times (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at_ts INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

def ensure_chat(chat_id: int):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO chat_settings(chat_id) VALUES (?)", (chat_id,))

def get_settings(chat_id: int):
    ensure_chat(chat_id)
    with db() as conn:
        row = conn.execute("SELECT * FROM chat_settings WHERE chat_id=?", (chat_id,)).fetchone()
        return row

def set_setting(chat_id: int, key: str, value):
    ensure_chat(chat_id)
    with db() as conn:
        conn.execute(f"UPDATE chat_settings SET {key}=? WHERE chat_id=?", (value, chat_id))

def add_whitelist(chat_id: int, domain: str):
    domain = domain.lower().strip()
    ensure_chat(chat_id)
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO whitelist_domains(chat_id, domain) VALUES (?,?)", (chat_id, domain))

def list_whitelist(chat_id: int):
    ensure_chat(chat_id)
    with db() as conn:
        rows = conn.execute("SELECT domain FROM whitelist_domains WHERE chat_id=? ORDER BY domain", (chat_id,)).fetchall()
        return [r["domain"] for r in rows]

def record_join(chat_id: int, user_id: int, ts: int):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO join_times(chat_id, user_id, joined_at_ts) VALUES (?,?,?)",
            (chat_id, user_id, ts),
        )

def get_join_ts(chat_id: int, user_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT joined_at_ts FROM join_times WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return row["joined_at_ts"] if row else None

# ================== HELPERS ==================
def is_adminish(member_status: str) -> bool:
    return member_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

def extract_domains(text: str):
    domains = []
    for m in URL_RE.finditer(text or ""):
        raw = m.group(0)
        if raw.lower().startswith("www."):
            raw = "http://" + raw
        if raw.lower().startswith("t.me/"):
            raw = "https://" + raw
        try:
            u = urlparse(raw)
            if u.netloc:
                domains.append(u.netloc.lower())
        except Exception:
            pass
    return domains

def domain_allowed(chat_id: int, domain: str) -> bool:
    wl = list_whitelist(chat_id)
    d = domain.lower()
    # allow exact or subdomain of whitelisted
    return any(d == w or d.endswith("." + w) for w in wl)

async def log_action(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    if not s or s["log_mode"] == "off":
        return
    # "here" means same chat
    try:
        await context.bot.send_message(chat_id=chat_id, text=f"🛡 {text}")
    except Exception:
        pass

async def restrict_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, minutes: int):
    until = datetime.now(TZ) + timedelta(minutes=minutes)
    perms = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
    )
    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=perms,
        until_date=until
    )

# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Живий ✅ Додай мене адміном у групу, і буде security.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡 Команди (працюють у групі, для адмінів):\n"
        "/status — налаштування\n"
        "/antilinks on|off — лінки\n"
        "/whitelist add example.com — whitelist домен\n"
        "/flood N WINDOW MUTE — напр: /flood 6 10 15\n"
        "/newbie MIN — напр: /newbie 15\n"
        "/log here|off — логи\n\n"
        "Потрібні права адміна: Delete + Restrict."
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    wl = list_whitelist(chat_id)
    msg = (
        f"⚙️ Settings:\n"
        f"- anti_links: {'ON' if s['anti_links'] else 'OFF'}\n"
        f"- flood: {s['flood_n']} msg / {s['flood_window_sec']} sec → mute {s['flood_mute_min']} min\n"
        f"- newbie protect: {s['newbie_protect_min']} min\n"
        f"- log: {s['log_mode']}\n"
        f"- whitelist: {', '.join(wl) if wl else '—'}"
    )
    await update.message.reply_text(msg)

async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    member = await context.bot.get_chat_member(chat.id, user.id)
    if not is_adminish(member.status):
        await update.message.reply_text("Тільки адміни 🫡")
        return False
    return True

async def antilinks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    parts = (update.message.text or "").split()
    if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
        await update.message.reply_text("Формат: /antilinks on|off")
        return
    set_setting(chat_id, "anti_links", 1 if parts[1].lower() == "on" else 0)
    await update.message.reply_text("Ок ✅")

async def whitelist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text("Формат: /whitelist add example.com")
        return
    if parts[1].lower() != "add" or len(parts) != 3:
        await update.message.reply_text("Формат: /whitelist add example.com")
        return
    add_whitelist(chat_id, parts[2])
    await update.message.reply_text("Додано ✅")

async def flood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    parts = (update.message.text or "").split()
    if len(parts) != 4 or not all(p.isdigit() for p in parts[1:]):
        await update.message.reply_text("Формат: /flood N WINDOW_SEC MUTE_MIN  (напр: /flood 6 10 15)")
        return
    n, win, mute = map(int, parts[1:])
    set_setting(chat_id, "flood_n", n)
    set_setting(chat_id, "flood_window_sec", win)
    set_setting(chat_id, "flood_mute_min", mute)
    await update.message.reply_text("Ок ✅")

async def newbie_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    parts = (update.message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Формат: /newbie MIN  (напр: /newbie 15)")
        return
    set_setting(chat_id, "newbie_protect_min", int(parts[1]))
    await update.message.reply_text("Ок ✅")

async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    parts = (update.message.text or "").split()
    if len(parts) != 2 or parts[1].lower() not in ("here", "off"):
        await update.message.reply_text("Формат: /log here|off")
        return
    set_setting(chat_id, "log_mode", parts[1].lower())
    await update.message.reply_text("Ок ✅")

# ================== EVENTS / SECURITY ==================
async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Track join time for newbie protection
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return
    chat_id = update.effective_chat.id
    ts = int(time.time())
    for u in msg.new_chat_members:
        record_join(chat_id, u.id, ts)

async def security_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.from_user:
        return

    chat_id = update.effective_chat.id
    user_id = msg.from_user.id

    # skip admins
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if is_adminish(member.status):
            return
    except Exception:
        pass

    s = get_settings(chat_id)
    if not s:
        return

    text = msg.text or msg.caption or ""

    # Newbie protection window
    newbie_min = int(s["newbie_protect_min"])
    join_ts = get_join_ts(chat_id, user_id)
    is_newbie = False
    if join_ts:
        is_newbie = (time.time() - join_ts) < newbie_min * 60

    # Anti-links (also stricter for newbies)
    if int(s["anti_links"]) == 1:
        domains = extract_domains(text)
        if domains:
            # allow whitelisted only
            if not all(domain_allowed(chat_id, d) for d in domains):
                try:
                    await msg.delete()
                except Exception:
                    return
                await log_action(update, context, f"Deleted link from @{msg.from_user.username or msg.from_user.id}")
                # if newbie posted link -> quick mute
                if is_newbie:
                    try:
                        await restrict_user(context, chat_id, user_id, minutes=int(s["flood_mute_min"]))
                        await log_action(update, context, f"Muted newbie for links ({s['flood_mute_min']} min)")
                    except Exception:
                        pass
                return

    # Extra newbie restrictions (optional, MVP): block forwards / media
    if is_newbie:
        if msg.forward_date is not None or msg.forward_origin is not None:
            try:
                await msg.delete()
                await log_action(update, context, f"Deleted forward from newbie @{msg.from_user.username or msg.from_user.id}")
            except Exception:
                pass
            return
        if msg.photo or msg.video or msg.document:
            try:
                await msg.delete()
                await log_action(update, context, f"Deleted media from newbie @{msg.from_user.username or msg.from_user.id}")
            except Exception:
                pass
            return

    # Flood control (count any message)
    n = int(s["flood_n"])
    win = int(s["flood_window_sec"])
    mute_min = int(s["flood_mute_min"])

    key = (chat_id, user_id)
    now = time.time()
    bucket = FLOOD_BUCKET.get(key, [])
    bucket = [t for t in bucket if now - t <= win]
    bucket.append(now)
    FLOOD_BUCKET[key] = bucket

    if len(bucket) > n:
        try:
            await restrict_user(context, chat_id, user_id, minutes=mute_min)
            await log_action(update, context, f"Muted for flood @{msg.from_user.username or msg.from_user.id} ({mute_min} min)")
        except Exception:
            pass

# ================== WEBHOOK SERVER ==================
app = Flask(__name__)
bot_app = Application.builder().token(TOKEN).build()

@app.route("/", methods=["GET"])
def index():
    return "Bot is alive"

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot_app.bot)
    bot_app.update_queue.put_nowait(update)
    return "ok"

def build_handlers(a: Application):
    a.add_handler(CommandHandler("start", start))
    a.add_handler(CommandHandler("help", help_cmd))
    a.add_handler(CommandHandler("status", status_cmd))
    a.add_handler(CommandHandler("antilinks", antilinks_cmd))
    a.add_handler(CommandHandler("whitelist", whitelist_cmd))
    a.add_handler(CommandHandler("flood", flood_cmd))
    a.add_handler(CommandHandler("newbie", newbie_cmd))
    a.add_handler(CommandHandler("log", log_cmd))

    # track joins
    a.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member))

    # main security filter (group only)
    a.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO | filters.Document.ALL), security_filter))

if __name__ == "__main__":
    init_db()
    build_handlers(bot_app)

    bot_app.initialize()
    bot_app.start()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
