import os
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

WEBHOOK_PATH = "/webhook"

# Telegram app
tg_app = Application.builder().token(TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Працюю ✅ (/start відповів)")

tg_app.add_handler(CommandHandler("start", start))

# Flask app
app = Flask(__name__)

@app.get("/")
def index():
    return "Bot is alive"

@app.post(WEBHOOK_PATH)
def webhook():
    update = Update.de_json(request.get_json(force=True), tg_app.bot)
    tg_app.update_queue.put_nowait(update)
    return "ok"

async def startup():
    await tg_app.initialize()
    await tg_app.start()

def main():
    asyncio.run(startup())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

if __name__ == "__main__":
    main()
