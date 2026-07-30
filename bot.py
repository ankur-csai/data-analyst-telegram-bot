"""Telegram entrypoint. Webhook mode on Render (RENDER_EXTERNAL_URL set),
long-polling fallback for local development.
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import agent
import logger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id
    text = message.text or ""
    if not text:
        return

    start = time.monotonic()
    real_log_url = logger.log_url()
    try:
        reply_text, trace = await asyncio.to_thread(agent.answer, chat_id, text, real_log_url)
    except Exception as e:
        log.exception("agent.answer failed")
        reply_text = "Sorry, I hit an internal error processing that."
        trace = [{"error": f"{type(e).__name__}: {e}"}]

    await message.reply_text(reply_text)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "incoming_text": text,
        "reply": reply_text,
        "tool_trace": trace,
        "latency_seconds": round(time.monotonic() - start, 2),
        "model": agent.MODEL,
    }
    try:
        await logger.append_log_line(record)
    except Exception:
        log.exception("failed to push log line to GitHub")


def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if external_url:
        port = int(os.environ.get("PORT", "10000"))
        log.info("starting webhook mode on port %s, url %s/%s", port, external_url, BOT_TOKEN)
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=BOT_TOKEN,
            webhook_url=f"{external_url}/{BOT_TOKEN}",
        )
    else:
        log.info("RENDER_EXTERNAL_URL not set, starting long-polling for local dev")
        application.run_polling()


if __name__ == "__main__":
    main()
