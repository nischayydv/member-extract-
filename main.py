import asyncio
import os
import json
import random
import time
import logging
import hashlib
from datetime import datetime
from threading import Thread
from queue import Queue
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import Application, CommandHandler, ConversationHandler, MessageHandler, filters, ContextTypes
from flask import Flask, render_template_string, jsonify, Response
import queue

app = Flask(__name__)
LOG_QUEUE = Queue(maxsize=1000)

class QueueHandler(logging.Handler):
    def emit(self, record):
        log_entry = {
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'level': record.levelname,
            'message': self.format(record)
        }
        try:
            LOG_QUEUE.put_nowait(log_entry)
        except queue.Full:
            try:
                LOG_QUEUE.get_nowait()
                LOG_QUEUE.put_nowait(log_entry)
            except:
                pass

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
queue_handler = QueueHandler()
queue_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
logging.getLogger().addHandler(queue_handler)

BOT_TOKEN = os.environ.get('BOT_TOKEN')
PORT = int(os.environ.get('PORT', 10000))

API_ID, API_HASH, PHONE, OTP_CODE, TWO_FA_PASSWORD, SOURCE, TARGET, INVITE_LINK = range(8)

USER_DATA_FILE = 'user_data.json'
STATS_FILE = 'stats.json'
DEVICE_INFO_FILE = 'device_info.json'

USER_HISTORY = {}
STATS = {}
ACTIVE_TASKS = {}
TEMP_CLIENTS = {}

@app.route('/')
def index():
    return render_template_string('<html><body><h1>Bot Running</h1></body></html>')

@app.route('/api/status')
def api_status():
    total_invited = 0
    total_dms = 0
    total_failed = 0
    for user_id, task in ACTIVE_TASKS.items():
        total_invited += task.get('invited_count', 0)
        total_dms += task.get('dm_count', 0)
        total_failed += task.get('failed_count', 0)
    return jsonify({'active_tasks': len(ACTIVE_TASKS), 'total_invited': total_invited, 'total_dms': total_dms, 'total_failed': total_failed})

@app.route('/api/logs/stream')
def logs_stream():
    def generate():
        while True:
            try:
                log = LOG_QUEUE.get(timeout=30)
                data = "data: " + json.dumps(log) + "

"
                yield data
            except queue.Empty:
                waiting = {'time': datetime.now().strftime('%H:%M:%S'), 'level': 'INFO', 'message': 'Waiting'}
                data = "data: " + json.dumps(waiting) + "

"
                yield data
            except Exception as e:
                logger.error(str(e))
                break
    return Response(generate(), mimetype='text/event-stream')

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'active_tasks': len(ACTIVE_TASKS), 'timestamp': datetime.now().isoformat()})

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is running! Use /run to start.")

async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Setup started!")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled")
    return ConversationHandler.END

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

async def main():
    logger.info("Starting bot...")
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    application = Application.builder().token(BOT_TOKEN).build()
    conv_handler = ConversationHandler(entry_points=[CommandHandler('run', run_command)], states={}, fallbacks=[CommandHandler('cancel', cancel)])
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('start', start))
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    while True:
        await asyncio.sleep(1)

if __name__ == '__main__':
    asyncio.run(main())
