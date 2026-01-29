import os
import asyncio
from flask import Flask, request

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, Update

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

WEBHOOK_PATH = "/webhook"

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Працюю ✅ (aiogram + webhook)")

app = Flask(__name__)

@app.get("/")
def index():
    return "Bot is alive"

@app.post(WEBHOOK_PATH)
def webhook():
    upd = Update.model_validate(request.get_json(force=True))
    asyncio.get_event_loop().create_task(dp.feed_update(bot, upd))
    return "ok"

async def startup():
    # aiogram не потребує окремого start для webhook-режиму
    pass

def main():
    asyncio.run(startup())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

if __name__ == "__main__":
    main()
