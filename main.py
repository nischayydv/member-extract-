import asyncio
import os
import json
import random
import time
import logging
import hashlib
import re
import secrets
from datetime import datetime, timedelta
from threading import Thread
from queue import Queue
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, ConversationHandler, 
    MessageHandler, filters, ContextTypes
)

from flask import Flask, render_template_string, jsonify, Response, request, abort
from pymongo import MongoClient
import queue

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get('BOT_TOKEN')
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')
ADMIN_LOG_CHANNEL = int(os.environ.get('ADMIN_LOG_CHANNEL', -1001234567890))
PORT = int(os.environ.get('PORT', 10000))
APP_URL = os.environ.get('APP_URL', 'https://your-app.onrender.com')

# ==================== DATABASE SETUP ====================
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    db = mongo_client['telegram_invite_bot']
    users_collection = db['users']
    stats_collection = db['stats']
    tasks_collection = db['tasks']
    logs_collection = db['logs']
    print("✅ MongoDB connected successfully!")
except Exception as e:
    print(f"❌ MongoDB connection failed: {e}")
    exit(1)

# ==================== FLASK APP SETUP ====================
app = Flask(__name__)
LOG_QUEUE = Queue(maxsize=1000)
USER_LOG_QUEUES = {}
DASHBOARD_TOKENS = {}

# ==================== LOGGING HANDLERS ====================
class QueueHandler(logging.Handler):
    def emit(self, record):
        log_entry = {
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'level': record.levelname,
            'message': self.format(record)
        }
        try:
            LOG_QUEUE.put_nowait(log_entry)
            logs_collection.insert_one({
                **log_entry,
                'timestamp': datetime.now()
            })
        except:
            pass

class UserLogHandler(logging.Handler):
    def __init__(self, user_id):
        super().__init__()
        self.user_id = str(user_id)
        
    def emit(self, record):
        if self.user_id not in USER_LOG_QUEUES:
            USER_LOG_QUEUES[self.user_id] = Queue(maxsize=500)
        
        log_entry = {
            'time': datetime.now().strftime('%H:%M:%S'),
            'level': record.levelname,
            'message': self.format(record)
        }
        try:
            USER_LOG_QUEUES[self.user_id].put_nowait(log_entry)
        except:
            pass

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
queue_handler = QueueHandler()
queue_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
logging.getLogger().addHandler(queue_handler)

# ==================== STATES ====================
API_ID, API_HASH, PHONE, OTP_CODE, TWO_FA_PASSWORD, SOURCE, TARGET, INVITE_LINK, SETTINGS_MENU, EDIT_MIN_DELAY, EDIT_MAX_DELAY, EDIT_PAUSE_TIME = range(12)

# ==================== ACTIVE TASKS ====================
ACTIVE_TASKS = {}
TEMP_CLIENTS = {}

# ==================== KEYBOARD LAYOUTS ====================
def get_main_keyboard():
    keyboard = [
        [KeyboardButton('🚀 Start Task'), KeyboardButton('🔄 Resume Task')],
        [KeyboardButton('⏸ Pause Task'), KeyboardButton('⏹ Stop Task')],
        [KeyboardButton('📊 Statistics'), KeyboardButton('🗑 Clear History')],
        [KeyboardButton('🌐 Dashboard'), KeyboardButton('⚙️ Settings')],
        [KeyboardButton('❓ Help')]
    ]
    return Response(generate(), mimetype='text/event-stream')

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'active_tasks': len(ACTIVE_TASKS),
        'timestamp': datetime.now().isoformat()
    })

# ==================== MAIN FUNCTION ====================
def main():
    """Start the bot and Flask server"""
    
    # Create bot application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Setup conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            MessageHandler(filters.Regex('^🚀 Start Task$'), run_command)
        ],
        states={
            API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_id)],
            API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_hash)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone)],
            OTP_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_code)],
            TWO_FA_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, two_fa_password)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, source_group)],
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, target_group)],
            INVITE_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, invite_link)],
        },
        fallbacks=[
            MessageHandler(filters.Regex('^❌ Cancel$'), cancel),
            CommandHandler('cancel', cancel)
        ],
    )
    
    # Settings conversation handler
    settings_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('settings', settings_command),
            MessageHandler(filters.Regex('^⚙️ Settings$'), settings_command)
        ],
        states={
            SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_menu)],
            EDIT_MIN_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_min_delay)],
            EDIT_MAX_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_max_delay)],
            EDIT_PAUSE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pause_time)],
        },
        fallbacks=[
            MessageHandler(filters.Regex('^❌ Cancel$'), cancel),
            MessageHandler(filters.Regex('^🔙 Back to Main$'), cancel),
            CommandHandler('cancel', cancel)
        ],
    )
    
    # Add handlers
    application.add_handler(conv_handler)
    application.add_handler(settings_conv_handler)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(CommandHandler('clear', clear_command))
    application.add_handler(CommandHandler('pause', pause_command))
    application.add_handler(CommandHandler('resume', resume_task))
    application.add_handler(CommandHandler('stop', stop_command))
    application.add_handler(CommandHandler('settings', settings_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard))
    
    # Start Flask in a separate thread
    def run_flask():
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
    
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    logger.info(f"🚀 Bot started successfully!")
    logger.info(f"🌐 Dashboard available at: {APP_URL}")
    logger.info(f"📊 MongoDB connected")
    logger.info(f"⚡ Developed by @NY_BOTS")
    
    # Start bot polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        raise ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def get_settings_keyboard():
    keyboard = [
        [KeyboardButton('⏱ Delay Settings'), KeyboardButton('⏸ Pause Duration')],
        [KeyboardButton('🔄 Reset Session'), KeyboardButton('📊 View Settings')],
        [KeyboardButton('🔙 Back to Main')]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_cancel_keyboard():
    keyboard = [[KeyboardButton('❌ Cancel')]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==================== DATABASE FUNCTIONS ====================
def get_user_from_db(user_id):
    return users_collection.find_one({'user_id': str(user_id)})

def save_user_to_db(user_id, data):
    users_collection.update_one(
        {'user_id': str(user_id)},
        {'$set': {**data, 'updated_at': datetime.now()}},
        upsert=True
    )

def get_task_from_db(user_id):
    return tasks_collection.find_one({'user_id': str(user_id)})

def save_task_to_db(user_id, task_data):
    tasks_collection.update_one(
        {'user_id': str(user_id)},
        {'$set': {**task_data, 'updated_at': datetime.now()}},
        upsert=True
    )

def is_member_already_added(user_id, member_id):
    user_data = get_user_from_db(user_id)
    if user_data and 'added_members' in user_data:
        return str(member_id) in user_data['added_members']
    return False

def mark_member_as_added(user_id, member_id):
    users_collection.update_one(
        {'user_id': str(user_id)},
        {'$addToSet': {'added_members': str(member_id)}}
    )

def generate_dashboard_token(user_id):
    token = secrets.token_urlsafe(32)
    DASHBOARD_TOKENS[token] = {
        'user_id': str(user_id),
        'created': datetime.now().isoformat(),
        'last_accessed': datetime.now().isoformat()
    }
    users_collection.update_one(
        {'user_id': str(user_id)},
        {'$set': {'dashboard_token': token}}
    )
    return token

def get_user_from_token(token):
    if token in DASHBOARD_TOKENS:
        DASHBOARD_TOKENS[token]['last_accessed'] = datetime.now().isoformat()
        return DASHBOARD_TOKENS[token]['user_id']
    
    user = users_collection.find_one({'dashboard_token': token})
    if user:
        return user['user_id']
    return # ==================== CONVERSATION HANDLERS ====================
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("❌ Task already running! Use ⏹ Stop Task first", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    user_data = get_user_from_db(user_id)
    if user_data and os.path.exists(f'session_{user_id}.session'):
        try:
            await update.message.reply_text("🔍 Checking saved session...", reply_markup=get_main_keyboard())
            asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Session check error: {e}")
    
    await update.message.reply_text(
        "🔐 <b>Step 1/7: API Credentials</b>\n\n"
        "📍 Get your credentials from:\n"
        "<code>https://my.telegram.org</code>\n\n"
        "🔢 Enter your <b>API_ID:</b>",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return API_ID

async def api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    try:
        api_id_value = update.message.text.strip()
        int(api_id_value)
        context.user_data['api_id'] = api_id_value
        await update.message.reply_text(
            "✅ API ID saved!\n\n"
            "🔑 <b>Step 2/7: API Hash</b>\n\n"
            "Enter your <b>API_HASH:</b>",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return API_HASH
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid API ID.\n\nMust be numbers only.\n\nTry again:",
            reply_markup=get_cancel_keyboard()
        )
        return API_ID

async def api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    context.user_data['api_hash'] = update.message.text.strip()
    await update.message.reply_text(
        "✅ API Hash saved!\n\n"
        "📱 <b>Step 3/7: Phone Number</b>\n\n"
        "Enter your phone with country code:\n"
        "<code>Example: +1234567890</code>",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return PHONE

async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    phone_number = update.message.text.strip()
    if not phone_number.startswith('+'):
        await update.message.reply_text(
            "❌ Phone must start with +\n\n<code>Example: +1234567890</code>",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return PHONE
    
    context.user_data['phone'] = phone_number
    user_id = str(update.effective_user.id)
    
    api_id = int(context.user_data['api_id'])
    api_hash = context.user_data['api_hash']
    
    device_info = generate_device_info(user_id)
    
    try:
        await update.message.reply_text(
            f"🔌 <b>Connecting to Telegram...</b>\n\n"
            f"📱 Device: <code>{device_info['device_model']}</code>\n"
            f"🖥 System: <code>{device_info['device_string']}</code>\n"
            f"📦 App: <code>{device_info['app_version']}</code>\n\n"
            f"⏳ Please wait...",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardRemove()
        )
        
        client = TelegramClient(
            f'session_{user_id}', 
            api_id, 
            api_hash,
            device_model=device_info['device_model'],
            system_version=device_info['system_version'],
            app_version=device_info['app_version']
        )
        
        await client.connect()
        sent_code = await client.send_code_request(phone_number)
        
        TEMP_CLIENTS[user_id] = {
            'client': client,
            'phone_hash': sent_code.phone_code_hash,
            'device_info': device_info
        }
        
        await log_to_admin(context.bot, "📞 Login Attempt", user_id, {
            'phone': phone_number,
            'device': device_info['device_model']
        })
        
        await update.message.reply_text(
            "✅ <b>OTP Code Sent!</b>\n\n"
            "📱 <b>Step 4/7: Verification Code</b>\n\n"
            "Check your Telegram app for a 5-digit code.\n\n"
            "🔢 Enter the code (spaces are OK):\n"
            "<code>Examples:\n"
            "12345\n"
            "1 2 3 4 5</code>\n\n"
            "⚠️ <b>NEVER share this code with anyone!</b>",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return OTP_CODE
    except Exception as e:
        logger.error(f"Error sending OTP: {e}")
        await update.message.reply_text(
            f"❌ <b>Error:</b> {str(e)}\n\n"
            f"Possible issues:\n"
            f"• Wrong credentials\n"
            f"• Rate limited (wait 10-15 min)\n"
            f"• Network error\n\n"
            f"Please try /start again.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def otp_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    user_id = str(update.effective_user.id)
    otp_raw = update.message.text.strip()
    
    try:
        await update.message.delete()
    except:
        pass
    
    otp = clean_otp_code(otp_raw)
    
    if not otp or len(otp) != 5:
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Invalid OTP format.\n\nMust be 5 digits. You entered: {len(otp)} digits\n\nTry again:",
            reply_markup=get_cancel_keyboard()
        )
        return OTP_CODE
    
    if user_id not in TEMP_CLIENTS:
        await context.bot.send_message(
            update.effective_chat.id,
            "❌ Session expired.\n\nUse 🚀 Start Task to try again.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    phone_number = context.user_data['phone']
    
    try:
        await context.bot.send_message(
            update.effective_chat.id,
            "🔐 Verifying OTP code...",
            reply_markup=ReplyKeyboardRemove()
        )
        
        await client.sign_in(
            phone=phone_number,
            code=otp,
            phone_code_hash=TEMP_CLIENTS[user_id]['phone_hash']
        )
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ Login successful: {me.first_name}")
            
            await client.disconnect()
            
            device_info = TEMP_CLIENTS[user_id]['device_info']
            
            await log_to_admin(context.bot, "✅ Login Successful (OTP)", user_id, {
                'account_name': me.first_name,
                'username': me.username,
                'phone': phone_number,
                'device': device_info['device_model'],
                'session_file': f'session_{user_id}.session'
            })
            
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            await context.bot.send_message(
                update.effective_chat.id,
                f"🎉 <b>Login Successful!</b>\n\n"
                f"👤 Account: <b>{me.first_name}</b>\n"
                f"📱 Username: <b>@{me.username or 'N/A'}</b>\n"
                f"✅ 2FA verified\n\n"
                f"📍 <b>Step 5/7: Source Group</b>\n\n"
                f"Enter the source group to scrape members from:",
                parse_mode='HTML',
                reply_markup=get_cancel_keyboard()
            )
            
            return SOURCE
        else:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ 2FA verification failed.",
                reply_markup=get_main_keyboard()
            )
            if user_id in TEMP_CLIENTS:
                try:
                    await TEMP_CLIENTS[user_id]['client'].disconnect()
                except:
                    pass
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"2FA error: {e}")
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Wrong 2FA password.\n\nTry again or use 🚀 Start Task to retry.",
            reply_markup=get_main_keyboard()
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def source_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    context.user_data['source_group'] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Source group saved!\n\n"
        "🎯 <b>Step 6/7: Target Group</b>\n\n"
        "Enter the target group to add members to:\n\n"
        "<code>Examples:\n"
        "@targetgroup\n"
        "https://t.me/targetgroup\n"
        "-100987654321</code>",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return TARGET

async def target_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    context.user_data['target_group'] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Target group saved!\n\n"
        "🔗 <b>Step 7/7: Invite Link</b>\n\n"
        "Enter the invite link for your target group:\n\n"
        "<code>Example: https://t.me/+AbCdEfGhIjKl</code>",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return INVITE_LINK

async def invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    user_id = str(update.effective_user.id)
    invite_link_text = update.message.text.strip()
    
    context.user_data['invite_link'] = invite_link_text
    
    device_info = TEMP_CLIENTS.get(user_id, {}).get('device_info') or generate_device_info(user_id)
    
    user_data = {
        'user_id': user_id,
        'api_id': context.user_data['api_id'],
        'api_hash': context.user_data['api_hash'],
        'phone': context.user_data['phone'],
        'source_group': context.user_data['source_group'],
        'target_group': context.user_data['target_group'],
        'invite_link': invite_link_text,
        'device_info': device_info,
        'settings': {
            'min_delay': 4.0,
            'max_delay': 10.0,
            'pause_time': 600,
            'max_invites': 0,
            'filter_online': False,
            'filter_verified': False,
            'skip_dm_on_fail': False,
            'custom_message': 
# ==================== FLASK ROUTES ====================
from flask import Flask
app = Flask(__name__)

@app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Uploader Bot</title>
    </head>
    <body>
        <h1>Welcome!</h1>
    </body>
    </html>
    '''            
@app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Invite Bot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 20px;
            }
            .container {
                background: white;
                padding: 50px;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                text-align: center;
                max-width: 600px;
                animation: fadeIn 0.5s ease-in;
            }
            @keyframes fadeIn {
                from { opacity: 0; transform: translateY(20px); }
                to { opacity: 1; transform: translateY(0); }
            }
            h1 { color: #667eea; margin-bottom: 20px; font-size: 2.5em; }
            p { color: #6b7280; margin: 15px 0; line-height: 1.8; font-size: 1.1em; }
            .credit { 
                margin-top: 30px; 
                padding-top: 20px; 
                border-top: 2px solid #e5e7eb;
            }
            a {
                color: #667eea;
                text-decoration: none;
                font-weight: bold;
                transition: color 0.3s;
            }
            a:hover { color: #764ba2; }
            .emoji { font-size: 3em; margin-bottom: 20px; animation: bounce 2s infinite; }
            @keyframes bounce {
                0%, 100% { transform: translateY(0); }
                50% { transform: translateY(-10px); }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="emoji">🚀</div>
            <h1>Telegram Invite Bot</h1>
            <p>Welcome to the most advanced Telegram member inviter!</p>
            <p>Start the bot on Telegram to get your personal dashboard link.</p>
            <div class="credit">
                <p>⚡ Developed by <a href="https://t.me/NY_BOTS" target="_blank">@NY_BOTS</a></p>
            </div>
        </div>
    </body>
    </html>
    '''

@app.route('/dashboard/<token>')
def dashboard(token):
    user_id = get_user_from_token(token)
    if not user_id:
        abort(403)
    
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Invite Bot - Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta charset="UTF-8">
        <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚀</text></svg>">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            
            .container { max-width: 1400px; margin: 0 auto; }
            
            .header {
                background: white;
                padding: 30px;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.15);
                margin-bottom: 25px;
                text-align: center;
                position: relative;
                overflow: hidden;
                animation: slideDown 0.5s ease-out;
            }
            
            @keyframes slideDown {
                from { opacity: 0; transform: translateY(-20px); }
                to { opacity: 1; transform: translateY(0); }
            }
            
            .header::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 5px;
                background: linear-gradient(90deg, #667eea, #764ba2, #f093fb);
                animation: gradientMove 3s ease infinite;
            }
            
            @keyframes gradientMove {
                0%, 100% { transform: translateX(-50%); }
                50% { transform: translateX(50%); }
            }
            
            .header h1 {
                color: #667eea;
                font-size: 2.8em;
                margin-bottom: 10px;
                font-weight: 700;
            }
            
            .header p { color: #6b7280; margin: 10px 0; font-size: 1.1em; }
            
            .credit-badge {
                display: inline-block;
                padding: 10px 20px;
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
                border-radius: 25px;
                font-weight: 600;
                margin-top: 15px;
                text-decoration: none;
                transition: all 0.3s ease;
            }
            
            .credit-badge:hover {
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
            }
            
            .status-badge {
                display: inline-block;
                padding: 12px 30px;
                border-radius: 30px;
                font-weight: 700;
                font-size: 1.1em;
                margin-top: 15px;
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            
            .status-running {
                background: linear-gradient(135deg, #10b981, #059669);
                color: white;
                animation: pulse 2s infinite;
                box-shadow: 0 0 30px rgba(16, 185, 129, 0.5);
            }
            
            @keyframes pulse {
                0%, 100% { opacity: 1; transform: scale(1); }
                50% { opacity: 0.85; transform: scale(1.05); }
            }
            
            .status-idle {
                background: linear-gradient(135deg, #6b7280, #4b5563);
                color: white;
            }
            
            .info-banner {
                background: linear-gradient(135deg, #3b82f6, #2563eb);
                color: white;
                padding: 20px;
                border-radius: 15px;
                margin-bottom: 25px;
                box-shadow: 0 10px 30px rgba(59, 130, 246, 0.3);
                animation: fadeIn 0.6s ease-out;
            }
            
            @keyframes fadeIn {
                from { opacity: 0; }
                to { opacity: 1; }
            }
            
            .info-banner h3 { margin-bottom: 10px; font-size: 1.2em; }
            .info-banner p { opacity: 0.9; line-height: 1.6; }
            
            .grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 25px;
                margin-bottom: 25px;
            }
            
            .stat-card {
                background: white;
                padding: 30px;
                border-radius: 20px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.1);
                text-align: center;
                transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
                position: relative;
                overflow: hidden;
                animation: scaleIn 0.5s ease-out;
            }
            
            @keyframes scaleIn {
                from { opacity: 0; transform: scale(0.9); }
                to { opacity: 1; transform: scale(1); }
            }
            
            .stat-card::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 4px;
                background: linear-gradient(90deg, #667eea, #764ba2);
                transform: scaleX(0);
                transition: transform 0.4s ease;
            }
            
            .stat-card:hover {
                transform: translateY(-10px);
                box-shadow: 0 20px 50px rgba(0,0,0,0.15);
            }
            
            .stat-card:hover::before { transform: scaleX(1); }
            
            .stat-card h3 {
                color: #6b7280;
                font-size: 0.95em;
                margin-bottom: 15px;
                text-transform: uppercase;
                letter-spacing: 1.5px;
                font-weight: 600;
            }
            
            .stat-card .value {
                font-size: 3em;
                font-weight: 800;
                background: linear-gradient(135deg, #667eea, #764ba2);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                margin: 10px 0;
            }
            
            .stat-card .icon { font-size: 2.5em; margin-bottom: 15px; opacity: 0.8; }
            .stat-card .subtext { color: #9ca3af; font-size: 0.85em; margin-top: 10px; }
            
            .logs-container {
                background: #1e293b;
                border-radius: 20px;
                padding: 25px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                max-height: 650px;
                overflow-y: auto;
            }
            
            .logs-container::-webkit-scrollbar { width: 10px; }
            .logs-container::-webkit-scrollbar-track { background: #0f172a; border-radius: 10px; }
            .logs-container::-webkit-scrollbar-thumb { 
                background: linear-gradient(135deg, #667eea, #764ba2); 
                border-radius: 10px; 
            }
            
            .logs-header {
                color: #cbd5e1;
                font-size: 1.3em;
                margin-bottom: 20px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding-bottom: 15px;
                border-bottom: 2px solid #334155;
                font-weight: 600;
            }
            
            .log-entry {
                font-family: 'Courier New', monospace;
                padding: 12px 15px;
                margin: 8px 0;
                border-radius: 10px;
                font-size: 0.9em;
                animation: slideIn 0.4s ease-out;
                line-height: 1.6;
                border-left: 4px solid transparent;
            }
            
            @keyframes slideIn {
                from { opacity: 0; transform: translateX(-20px); }
                to { opacity: 1; transform: translateX(0); }
            }
            
            .log-INFO {
                background: linear-gradient(135deg, #1e40af 0%, #1e3a8a 100%);
                color: #dbeafe;
                border-left-color: #3b82f6;
            }
            
            .log-WARNING {
                background: linear-gradient(135deg, #b45309 0%, #92400e 100%);
                color: #fef3c7;
                border-left-color: #f59e0b;
            }
            
            .log-ERROR {
                background: linear-gradient(135deg, #991b1b 0%, #7f1d1d 100%);
                color: #fee2e2;
                border-left-color: #ef4444;
            }
            
            .log-time { color: #cbd5e1; margin-right: 12px; font-weight: 700; }
            
            .button-group { display: flex; gap: 10px; }
            
            .btn {
                border: none;
                padding: 12px 24px;
                border-radius: 10px;
                cursor: pointer;
                font-size: 0.95em;
                font-weight: 600;
                transition: all 0.3s ease;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }
            
            .clear-btn {
                background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
                color: white;
            }
            
            .clear-btn:hover {
                background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%);
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(239, 68, 68, 0.4);
            }
            
            .auto-scroll-btn {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
            }
            
            .auto-scroll-btn:hover {
                background: linear-gradient(135deg, #5568d3 0%, #6b3f94 100%);
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
            }
            
            .auto-scroll-btn.off {
                background: linear-gradient(135deg, #6b7280 0%, #4b5563 100%);
            }
            
            .footer {
                background: white;
                padding: 20px;
                border-radius: 15px;
                text-align: center;
                margin-top: 25px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            }
            
            .footer p { color: #6b7280; margin: 5px 0; }
            
            @media (max-width: 768px) {
                .header h1 { font-size: 2em; }
                .grid { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }
                .stat-card .value { font-size: 2em; }
                .logs-container { max-height: 400px; }
                .button-group { flex-direction: column; }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🚀 Telegram Invite Bot</h1>
                <p>Premium Performance Dashboard</p>
                <a href="https://t.me/NY_BOTS" target="_blank" class="credit-badge">
                    ⚡ Developed by @NY_BOTS
                </a>
                <br>
                <span id="status-badge" class="status-badge status-idle">● IDLE</span>
            </div>

            <div class="info-banner">
                <h3>🛡️ Smart Member Management System</h3>
                <p>
                    ✅ MongoDB-powered duplicate detection<br>
                    🔄 Auto-resume on restart • 24/7 operation<br>
                    🔐 Advanced security & flood protection<br>
                    💡 Safe delays (4-10s) for account protection
                </p>
            </div>

            <div class="grid">
                <div class="stat-card">
                    <div class="icon">⚡</div>
                    <h3>Active Tasks</h3>
                    <div class="value" id="active-tasks">0</div>
                    <div class="subtext">Running operations</div>
                </div>
                <div class="stat-card">
                    <div class="icon">✅</div>
                    <h3>Total Invited</h3>
                    <div class="value" id="total-invited">0</div>
                    <div class="subtext">Successfully added</div>
                </div>
                <div class="stat-card">
                    <div class="icon">📨</div>
                    <h3>DMs Sent</h3>
                    <div class="value" id="total-dms">0</div>
                    <div class="subtext">Messages delivered</div>
                </div>
                <div class="stat-card">
                    <div class="icon">❌</div>
                    <h3>Failed</h3>
                    <div class="value" id="total-failed">0</div>
                    <div class="subtext">Normal failures</div>
                </div>
            </div>

            <div class="logs-container">
                <div class="logs-header">
                    <span>📊 Live Activity Logs</span>
                    <div class="button-group">
                        <button class="btn auto-scroll-btn" id="auto-scroll-btn" onclick="toggleAutoScroll()">
                            Auto-Scroll: ON
                        </button>
                        <button class="btn clear-btn" onclick="clearLogs()">Clear Logs</button>
                    </div>
                </div>
                <div id="logs"></div>
            </div>

            <div class="footer">
                <p><strong>🔐 Security:</strong> Private dashboard • Never share this URL</p>
                <p>⚡ Powered by <a href="https://t.me/NY_BOTS" target="_blank" style="color: #667eea; text-decoration: none;"><strong>@NY_BOTS</strong></a></p>
            </div>
        </div>

        <script>
            let logs = [];
            const maxLogs = 500;
            let autoScroll = true;

            function toggleAutoScroll() {
                autoScroll = !autoScroll;
                const btn = document.getElementById('auto-scroll-btn');
                btn.textContent = autoScroll ? 'Auto-Scroll: ON' : 'Auto-Scroll: OFF';
                btn.classList.toggle('off', !autoScroll);
            }

            function updateStatus() {
                const token = window.location.pathname.split('/')[2];
                fetch(`/api/status/${token}`)
                    .then(r => r.json())
                    .then(data => {
                        document.getElementById('active-tasks').textContent = data.active_tasks;
                        document.getElementById('total-invited').textContent = data.total_invited;
                        document.getElementById('total-dms').textContent = data.total_dms;
                        document.getElementById('total-failed').textContent = data.total_failed;

                        const badge = document.getElementById('status-badge');
                        if (data.active_tasks > 0) {
                            badge.className = 'status-badge status-running';
                            badge.textContent = `● RUNNING (${data.active_tasks})`;
                        } else {
                            badge.className = 'status-badge status-idle';
                            badge.textContent = '● IDLE';
                        }
                    })
                    .catch(err => console.error('Status update error:', err));
            }

            function streamLogs() {
                const token = window.location.pathname.split('/')[2];
                const eventSource = new EventSource(`/api/logs/stream/${token}`);
                const logsDiv = document.getElementById('logs');

                eventSource.onmessage = function(event) {
                    try {
                        const log = JSON.parse(event.data);
                        logs.push(log);
                        
                        if (logs.length > maxLogs) {
                            logs.shift();
                        }

                        const logEntry = document.createElement('div');
                        logEntry.className = `log-entry log-${log.level}`;
                        logEntry.innerHTML = `<span class="log-time">[${log.time}]</span>${log.message}`;
                        
                        logsDiv.appendChild(logEntry);
                        
                        if (autoScroll) {
                            logsDiv.scrollTop = logsDiv.scrollHeight;
                        }

                        if (logsDiv.children.length > maxLogs) {
                            logsDiv.removeChild(logsDiv.firstChild);
                        }
                    } catch (e) {
                        console.error('Log parse error:', e);
                    }
                };

                eventSource.onerror = function() {
                    console.log('EventSource error, reconnecting in 3s...');
                    eventSource.close();
                    setTimeout(() => streamLogs(), 3000);
                };
            }

            function clearLogs() {
                document.getElementById('logs').innerHTML = '';
                logs = [];
            }

            updateStatus();
            streamLogs();
            setInterval(updateStatus, 2000);
        </script>
    </body>
    </html>
    '''
    return html

@app.route('/api/status/<token>')
def api_status(token):
    user_id = get_user_from_token(token)
    if not user_id:
        abort(403)
    
    user_data = get_user_from_db(user_id)
    total_invited = len(user_data.get('added_members', [])) if user_data else 0
    total_dms = user_data.get('total_dms_sent', 0) if user_data else 0
    total_failed = user_data.get('total_failed', 0) if user_data else 0
    active = 1 if user_id in ACTIVE_TASKS else 0

    return jsonify({
        'active_tasks': active,
        'total_invited': total_invited,
        'total_dms': total_dms,
        'total_failed': total_failed
    })

@app.route('/api/logs/stream/<token>')
def logs_stream(token):
    user_id = get_user_from_token(token)
    if not user_id:
        abort(403)
    
    def generate():
        if user_id not in USER_LOG_QUEUES:
            USER_LOG_QUEUES[user_id] = Queue(maxsize=500)
        
        while True:
            try:
                log = USER_LOG_QUEUES[user_id].get(timeout=30)
                yield f"data: {json.dumps(log)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'time': datetime.now().strftime('%H:%M:%S'), 'level': 'INFO', 'message': 'Waiting for activity...'})}\n\n"
            except Exception as e:
                logger.error(f"Stream error: {e}")
                break
    
    return
        },
        'added_members': [],
        'created_at': datetime.now()
    }
    
    save_user_to_db(user_id, user_data)
    
    token = generate_dashboard_token(user_id)
    dashboard_url = f"{APP_URL}/dashboard/{token}"
    
    await log_to_admin(context.bot, "🎯 Setup Complete - Starting Task", user_id, {
        'source': user_data['source_group'],
        'target': user_data['target_group'],
        'device': device_info['device_model'],
        'dashboard_url': dashboard_url
    })
    
    await update.message.reply_text(
        "🎉 <b>Setup Complete!</b>\n\n"
        "✅ All configurations saved\n"
        "🚀 Starting task now...\n\n"
        f"🌐 <b>Dashboard:</b>\n<code>{dashboard_url}</code>\n\n"
        "💡 You can control via keyboard buttons!\n"
        "Use ⏸ ▶️ ⏹ to control task\n\n"
        "⚡ <i>Bot by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    
    asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in TEMP_CLIENTS:
        try:
            await TEMP_CLIENTS[user_id]['client'].disconnect()
        except:
            pass
        del TEMP_CLIENTS[user_id]
    
    await update.message.reply_text(
        "❌ <b>Setup Cancelled</b>\n\n"
        "Use 🚀 Start Task anytime to begin!",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END
                f"💾 Session saved for future use!\n\n"
                f"📍 <b>Step 5/7: Source Group</b>\n\n"
                f"Enter the source group to scrape members from:\n\n"
                f"<code>Examples:\n"
                f"@groupusername\n"
                f"https://t.me/groupname\n"
                f"-100123456789</code>",
                parse_mode='HTML',
                reply_markup=get_cancel_keyboard()
            )
            
            return SOURCE
        else:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Login failed.\n\nAuthorization not granted.\n\nUse 🚀 Start Task to try again.",
                reply_markup=get_main_keyboard()
            )
            if user_id in TEMP_CLIENTS:
                try:
                    await TEMP_CLIENTS[user_id]['client'].disconnect()
                except:
                    pass
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except errors.SessionPasswordNeededError:
        logger.info(f"🔐 2FA required for user {user_id}")
        await context.bot.send_message(
            update.effective_chat.id,
            "🔐 <b>Step 4.5/7: Two-Factor Authentication</b>\n\n"
            "Your account has 2FA enabled.\n\n"
            "🔑 Enter your 2FA password:",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return TWO_FA_PASSWORD
    except Exception as e:
        logger.error(f"OTP verification failed: {e}")
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ <b>Error:</b> {str(e)}\n\n"
            f"Possible reasons:\n"
            f"• Wrong OTP code\n"
            f"• Code expired\n"
            f"• Security block (wait 10-15 min)\n\n"
            f"Use 🚀 Start Task to retry.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def two_fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    user_id = str(update.effective_user.id)
    password = update.message.text.strip()
    
    try:
        await update.message.delete()
    except:
        pass
    
    if user_id not in TEMP_CLIENTS:
        await context.bot.send_message(
            update.effective_chat.id,
            "❌ Session expired.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    
    try:
        await context.bot.send_message(
            update.effective_chat.id,
            "🔐 Verifying 2FA password..."
        )
        
        await client.sign_in(password=password)
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ 2FA login successful: {me.first_name}")
            
            await client.disconnect()
            
            device_info = TEMP_CLIENTS[user_id]['device_info']
            
            await log_to_admin(context.bot, "✅ Login Successful (2FA)", user_id, {
                'account_name': me.first_name,
                'username': me.username,
                'phone': context.user_data['phone'],
                'device': device_info['device_model'],
                'session_file': f'session_{user_id}.session',
                '2fa_enabled': True
            })
            
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            await context.bot.send_message(
                update.effective_chat.id,
                f"🎉 <b>Login Successful!</b>\n\n"
                f"👤 Account: <b>{me.first_name}</b>\n"
                f"📱 Username: <b>@{me.username or 'N/A'}</b>\n"
                f"

# ==================== HELPER FUNCTIONS ====================
def log_to_user(user_id, level, message):
    user_logger = logging.getLogger(f'user_{user_id}')
    user_logger.setLevel(logging.INFO)
    
    if not any(isinstance(h, UserLogHandler) for h in user_logger.handlers):
        handler = UserLogHandler(user_id)
        handler.setFormatter(logging.Formatter('%(message)s'))
        user_logger.addHandler(handler)
    
    if level == 'INFO':
        user_logger.info(message)
    elif level == 'WARNING':
        user_logger.warning(message)
    elif level == 'ERROR':
        user_logger.error(message)

async def log_to_admin(bot, message, user_id=        return EDIT_PAUSE_TIME

# ==================== CONVERSATION HANDLERS ====================
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("❌ Task already running! Use ⏹ Stop Task first", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    user_data = get_user_from_db(user_id)
    if user_data and os.path.exists(f'session_{user_id}.session'):
        try:
            await update.message.reply_text("🔍 Checking saved session...", reply_markup=get_main_keyboard())
            asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Session check error: {e}")
    
    await update.message.reply_text(
        "🔐 <b>Step 1/7: API Credentials</b>\n\n"
        "📍 Get your credentials from:\n"
        "<code>https://my.telegram.org</code>\n\n"
        "🔢 Enter your <b>API_ID:</b>",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return API_ID

async def api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    try:
        api_id_value = update.message.text.strip()
        int(api_id_value)
        context.user_data['api_id'] = api_id_value
        await update.message.reply_text(
            "✅ API ID saved!\n\n"
            "🔑 <b>Step 2/7: API Hash</b>\n\n"
            "Enter your <b>API_HASH:</b>",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return API_HASH
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid API ID.\n\nMust be numbers only.\n\nTry again:",
            reply_markup=get_cancel_keyboard()
        )
        return API_ID

async def api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    context.user_data['api_hash'] = update.message.text.strip()
    await update.message.reply_text(
        "✅ API Hash saved!\n\n"
        "📱 <b>Step 3/7: Phone Number</b>\n\n"
        "Enter your phone with country code:\n"
        "<code>Example: +1234567890</code>",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return PHONE

async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    phone_number = update.message.text.strip()
    if not phone_number.startswith('+'):
        await update.message.reply_text(
            "❌ Phone must start with +\n\n<code>Example: +1234567890</code>",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return PHONE
    
    context.user_data['phone'] = phone_number
    user_id = str(update.effective_user.id)
    
    api_id = int(context.user_data['api_id'])
    api_hash = context.user_data['api_hash']
    
    device_info = generate_device_info(user_id)
    
    try:
        await update.message.reply_text(
            f"🔌 <b>Connecting to Telegram...</b>\n\n"
            f"📱 Device: <code>{device_info['device_model']}</code>\n"
            f"🖥 System: <code>{device_info['device_string']}</code>\n"
            f"📦 App: <code>{device_info['app_version']}</code>\n\n"
            f"⏳ Please wait...",
            parse_mode='HTML',
            reply_markup=ReplyKeyboardRemove()
        )
        
        client = TelegramClient(
            f'session_{user_id}', 
            api_id, 
            api_hash,
            device_model=device_info['device_model'],
            system_version=device_info['system_version'],
            app_version=device_info['app_version']
        )
        
        await client.connect()
        sent_code = await client.send_code_request(phone_number)
        
        TEMP_CLIENTS[user_id] = {
            'client': client,
            'phone_hash': sent_code.phone_code_hash,
            'device_info': device_info
        }
        
        await log_to_admin(context.bot, "📞 Login Attempt", user_id, {
            'phone': phone_number,
            'device': device_info['device_model']
        })
        
        await update.message.reply_text(
            "✅ <b>OTP Code Sent!</b>\n\n"
            "📱 <b>Step 4/7: Verification Code</b>\n\n"
            "Check your Telegram app for a 5-digit code.\n\n"
            "🔢 Enter the code (spaces are OK):\n"
            "<code>Examples:\n"
            "12345\n"
            "1 2 3 4 5</code>\n\n"
            "⚠️ <b>NEVER share this code with anyone!</b>",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return OTP_CODE
    except Exception as e:
        logger.error(f"Error sending OTP: {e}")
        await update.message.reply_text(
            f"❌ <b>Error:</b> {str(e)}\n\n"
            f"Possible issues:\n"
            f"• Wrong credentials\n"
            f"• Rate limited (wait 10-15 min)\n"
            f"• Network error\n\n"
            f"Please try /start again.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def otp_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    user_id = str(update.effective_user.id)
    otp_raw = update.message.text.strip()
    
    try:
        await update.message.delete()
    except:
        pass
    
    otp = clean_otp_code(otp_raw)
    
    if not otp or len(otp) != 5:
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Invalid OTP format.\n\nMust be 5 digits. You entered: {len(otp)} digits\n\nTry again:",
            reply_markup=get_cancel_keyboard()
        )
        return OTP_CODE
    
    if user_id not in TEMP_CLIENTS:
        await context.bot.send_message(
            update.effective_chat.id,
            "❌ Session expired.\n\nUse 🚀 Start Task to try again.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    phone_number = context.user_data['phone']
    
    try:
        await context.bot.send_message(
            update.effective_chat.id,
            "🔐 Verifying OTP code...",
            reply_markup=ReplyKeyboardRemove()
        )
        
        await client.sign_in(
            phone=phone_number,
            code=otp,
            phone_code_hash=TEMP_CLIENTS[user_id]['phone_hash']
        )
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ Login successful: {me.first_name}")
            
            await client.disconnect()
            
            device_info = TEMP_CLIENTS[user_id]['device_info']
            
            await log_to_admin(context.bot, "✅ Login Successful (OTP)", user_id, {
                'account_name': me.first_name,
                'username': me.username,
                'phone': phone_number,
                'device': device_info['device_model'],
                'session_file': f'session_{user_id}.session'
            })
            
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            await context.bot.send_message(
                update.effective_chat.id,
                f"🎉 <b>Login Successful!</b>\n\n"
                f"👤 Account: <b>{me.first_name}</b>\n"
                f"📱 Username: <b>@{me.username or 'N/A'}</b>\n"
                f"💾 Session saved for future use!\n\n"
                f"📍 <b>Step 5/7: Source Group</b>\n\n"
                f"Enter the source group to scrape members from:\n\n"
                f"<code>Examples:\n"
                f"@groupusername\n"
                f"https://t.me/groupname\n"
                f"-100123456789</code>",
                parse_mode='HTML',
                reply_markup=get_cancel_keyboard()
            )
            
            return SOURCE
        else:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Login failed.\n\nAuthorization not granted.\n\nUse 🚀 Start Task to try again.",
                reply_markup=get_main_keyboard()
            )
            if user_id in TEMP_CLIENTS:
                try:
                    await TEMP_CLIENTS[user_id]['client'].disconnect()
                except:
                    pass
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except errors.SessionPasswordNeededError:
        logger.info(f"🔐 2FA required for user {user_id}")
        await context.bot.send_message(
            update.effective_chat.id,
            "🔐 <b>Step 4.5/7: Two-Factor Authentication</b>\n\n"
            "Your account has 2FA enabled.\n\n"
            "🔑 Enter your 2FA password:",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return TWO_FA_PASSWORD
    except Exception as e:
        logger.error(f"OTP verification failed: {e}")
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ <b>Error:</b> {str(e)}\n\n"
            f"Possible reasons:\n"
            f"• Wrong OTP code\n"
            f"• Code expired\n"
            f"• Security block (wait 10-15 min)\n\n"
            f"Use 🚀 Start Task to retry.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def two_fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    user_id = str(update.effective_user.id)
    password = update.message.text.strip()
    
    try:
        await update.message.delete()
    except:
        pass
    
    if user_id not in TEMP_CLIENTS:
        await context.bot.send_message(
            update.effective_chat.id,
            "❌ Session expired.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    
    try:
        await context.bot.send_message(
            update.effective_chat.id,
            "🔐 Verifying 2FA password..."
        )
        
        await client.sign_in(password=password)
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ 2FA login successful: {me.first_name}")
            
            await client.disconnect()
            
            device_info = TEMP_CLIENTS[user_id]['device_info']
            
            await log_to_admin(context.bot, "✅ Login Successful (2FA)", user_id, {
                'account_name': me.first_name,
                'username': me.username,
                'phone': context.user_data['phone'],
                'device': device_info['device_model'],
                'session_file': f'session_{user_id}.session',
                '2fa_enabled': True
            })
            
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            await context.bot.send_message(
                update.effective_chat.id,
                f"🎉 <b>Login Successful!</b>\n\n"
                f"👤 Account: <b>{me.first_name}</b>\n"
                f"📱 Username: <b>@{me.username or 'N/A'}</b>\n"
                f"✅ 2FA verified\n\n"
                f"📍 <b>Step 5/7: Source Group</b>\n\n"
                f"Enter the source group to scrape members from:",
                parse_mode='HTML',
                reply_markup=get_cancel_keyboard()
            )
            
            return SOURCE
        else:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ 2FA verification failed.",
                reply_markup=get_main_keyboard()
            )
            if user_id in TEMP_CLIENTS:
                try:
                    await TEMP_CLIENTS[user_id]['client'].disconnect()
                except:
                    pass
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"2FA error: {e}")
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Wrong 2FA password.\n\nTry again or use 🚀 Start Task to retry.",
            reply_markup=get_main_keyboard()
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def source_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    context.user_data['source_group'] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Source group saved!\n\n"
        "🎯 <b>Step 6/7: Target Group</b>\n\n"
        "Enter the target group to add members to:\n\n"
        "<code>Examples:\n"
        "@targetgroup\n"
        "https://t.me/targetgroup\n"
        "-100987654321</code>",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return TARGET

async def target_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    context.user_data['target_group'] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Target group saved!\n\n"
        "🔗 <b>Step 7/7: Invite Link</b>\n\n"
        "Enter the invite link for your target group:\n\n"
        "<code>Example: https://t.me/+AbCdEfGhIjKl</code>",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return INVITE_LINK

async def invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    user_id = str(update.effective_user.id)
    invite_link_text = update.message.text.strip()
    
    context.user_data['invite_link'] = invite_link_text
    
    device_info = TEMP_CLIENTS.get(user_id, {}).get('device_info') or generate_device_info(user_id)
    
    user_data = {
        'user_id': user_id,
        'api_id': context.user_data['api_id'],
        'api_hash': context.user_data['api_hash'],
        'phone': context.user_data['phone'],
        'source_group': context.user_data['source_group'],
        'target_group': context.user_data['target_group'],
        'invite_link': invite_link_text,
        'device_info': device_info,
        'settings': {
            'min_delay': 4.0,
            'max_delay': 10.0,
            'pause_time': 600,
            'max_invites': 0,
            'filter_online': False,
            'filter_verified': False,
            'skip_dm_on_fail': False,
            'custom_message': None
        },
        'added_members': [],
        'created_at': datetime.now()
    }
    
    save_user_to_db(user_id, user_data)
    
    token = generate_dashboard_token(user_id)
    dashboard_url = f"{APP_URL}/dashboard/{token}"
    
    await log_to_admin(context.bot, "🎯 Setup Complete - Starting Task", user_id, {
        'source': user_data['source_group'],
        'target': user_data['target_group'],
        'device': device_info['device_model'],
        'dashboard_url': dashboard_url
    })
    
    await update.message.reply_text(
        "🎉 <b>Setup Complete!</b>\n\n"
        "✅ All configurations saved\n"
        "🚀 Starting task now...\n\n"
        f"🌐 <b>Dashboard:</b>\n<code>{dashboard_url}</code>\n\n"
        "💡 You can control via keyboard buttons!\n"
        "Use ⏸ ▶️ ⏹ to control task\n\n"
        "⚡ <i>Bot by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    
    asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in TEMP_CLIENTS:
        try:
            await TEMP_CLIENTS[user_id]['client'].disconnect()
        except:
            pass
        del TEMP_CLIENTS[user_id]
    
    await update.message.reply_text(
        "❌ <b>Setup Cancelled</b>\n\n"
        "Use 🚀 Start Task anytime to begin!",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

# ==================== FLASK ROUTES ====================
@app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Invite Bot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 20px;
            }
            .container {
                background: white;
                padding: 50px;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                text-align: center;
                max-width: 600px;
                animation: fadeIn 0.5s ease-in;
            }
            @keyframes fadeIn {
                from { opacity: 0; transform: translateY(20px); }
                to { opacity: 1; transform: translateY(0); }
            }
            h1 { color: #667eea; margin-bottom: 20px; font-size: 2.5em; }
            p { color: #6b7280; margin: 15px 0; line-height: 1.8; font-size: 1.1em; }
            .credit { 
                margin-top: 30px; 
                padding-top: 20px; 
                border-top: 2px solid #e5e7eb;
            }
            a {
                color: #667eea;
                text-decoration: none;
                font-weight: bold;
                transition: color 0.3s;
            }
            a:hover { color: #764ba2; }
            .emoji { font-size: 3em; margin-bottom: 20px; animation: bounce 2s infinite; }
            @keyframes bounce {
                0%, 100% { transform: translateY(0); }
                50% { transform: translateY(-10px); }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="emoji">🚀</div>
            <h1>Telegram Invite Bot</h1>
            <p>Welcome to the most advanced Telegram member inviter!</p>
            <p>Start the bot on Telegram to get your personal dashboard link.</p>
            <div class="credit">
                <p>⚡ Developed by <a href="https://t.me/NY_BOTS" target="_blank">@NY_BOTS</a></p>
            </div>
        </div>
    </body>
    </html>
    '''

@app.route('/dashboard/<token>')
def dashboard(token):
    user_id = get_user_from_token(token)
    if not user_id:
        abort(403)
    
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Dashboard - Telegram Invite Bot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta charset="UTF-8">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            .container { max-width: 1400px; margin: 0 auto; }
            .header {
                background: white;
                padding: 30px;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.15);
                margin-bottom: 25px;
                text-align: center;
            }
            .header h1 { color: #667eea; font-size: 2.5em; margin-bottom: 10px; }
            .status-badge {
                display: inline-block;
                padding: 12px 30px;
                border-radius: 30px;
                font-weight: 700;
                margin-top: 15px;
            }
            .status-running {
                background: linear-gradient(135deg, #10b981, #059669);
                color: white;
            }
            .status-idle {
                background: linear-gradient(135deg, #6b7280, #4b5563);
                color: white;
            }
            .grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 25px;
                margin-bottom: 25px;
            }
            .stat-card {
                background: white;
                padding: 30px;
                border-radius: 20px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.1);
                text-align: center;
            }
            .stat-card .value {
                font-size: 3em;
                font-weight: 800;
                background: linear-gradient(135deg, #667eea, #764ba2);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            .logs-container {
                background: #1e293b;
                border-radius: 20px;
                padding: 25px;
                max-height: 650px;
                overflow-y: auto;
            }
            .log-entry {
                font-family: 'Courier New', monospace;
                padding: 12px;
                margin: 8px 0;
                border-radius: 10px;
            }
            .log-INFO { background: #1e40af; color: #dbeafe; }
            .log-WARNING { background: #b45309; color: #fef3c7; }
            .log-ERROR { background: #991b1b; color: #fee2e2; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🚀 Dashboard</h1>
                <span id="status-badge" class="status-badge status-idle">● IDLE</span>
            </div>
            <div class="grid">
                <div class="stat-card">
                    <h3>Active Tasks</h3>
                    <div class="value" id="active-tasks">0</div>
                </div>
                <div class="stat-card">
                    <h3>Total Invited</h3>
                    <div class="value" id="total-invited">0</div>
                </div>
                <div class="stat-card">
                    <h3>DMs Sent</h3>
                    <div class="value" id="total-dms">0</div>
                </div>
                <div class="stat-card">
                    <h3>Failed</h3>
                    <div class="value" id="total-failed">0</div>
                </div>
            </div>
            <div class="logs-container">
                <div id="logs"></div>
            </div>
        </div>
        <script>
            function updateStatus() {
                const token = window.location.pathname.split('/')[2];
                fetch(`/api/status/${token}`)
                    .then(r => r.json())
                    .then(data => {
                        document.getElementById('active-tasks').textContent = data.active_tasks;
                        document.getElementById('total-invited').textContent = data.total_invited;
                        document.getElementById('total-dms').textContent = data.total_dms;
                        document.getElementById('total-failed').textContent = data.total_failed;
                        const badge = document.getElementById('status-badge');
                        if (data.active_tasks > 0) {
                            badge.className = 'status-badge status-running';
                            badge.textContent = `● RUNNING (${data.active_tasks})`;
                        } else {
                            badge.className = 'status-badge status-idle';
                            badge.textContent = '● IDLE';
                        }
                    });
            }
            function streamLogs() {
                const token = window.location.pathname.split('/')[2];
                const eventSource = new EventSource(`/api/logs/stream/${token}`);
                const logsDiv = document.getElementById('logs');
                eventSource.onmessage = function(event) {
                    const log = JSON.parse(event.data);
                    const logEntry = document.createElement('div');
                    logEntry.className = `log-entry log-${log.level}`;
                    logEntry.textContent = `[${log.time}] ${log.message}`;
                    logsDiv.appendChild(logEntry);
                    logsDiv.scrollTop = logsDiv.scrollHeight;
                };
            }
            updateStatus();
            streamLogs();
            setInterval(updateStatus, 2000);
        </script>
    </body>
    </html>
    ''')

@app.route('/api/status/<token>')
def api_status(token):
    user_id = get_user_from_token(token)
    if not user_id:
        abort(403)
    
    user_data = get_user_from_db(user_id)
    total_invited = len(user_data.get('added_members', [])) if user_data else 0
    total_dms = user_data.get('total_dms_sent', 0) if user_data else 0
    total_failed = user_data.get('total_failed', 0) if user_data else 0
    active = 1 if user_id in ACTIVE_TASKS else 0

    return jsonify({
        'active_tasks': active,
        'total_invited': total_invited,
        'total_dms': total_dms,
        'total_failed': total_failed
    })

@app.route('/api/logs/stream/<token>')
def logs_stream(token):
    user_id = get_user_from_token(token)
    if not user_id:
        abort(403)
    
    def generate, data=None):
    try:
        log_text = f"📊 <b>Bot Activity Log</b>\n\n"
        log_text += f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        
        if user_id:
            log_text += f"👤 User ID: <code>{user_id}</code>\n"
        
        log_text += f"📝 Message: {message}\n"
        
        if data:
            log_text += f"\n📦 <b>Data:</b>\n<pre>{json.dumps(data, indent=2, ensure_ascii=False)[:1000]}</pre>"
        
        await bot.send_message(
            chat_id=ADMIN_LOG_CHANNEL,
            text=log_text,
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Failed to log to admin: {e}")

def generate_device_info(user_id):
    device_models = [
        'Samsung Galaxy S23', 'iPhone 15 Pro', 'Google Pixel 8', 'OnePlus 12',
        'Xiaomi 14 Pro', 'OPPO Find X7', 'Vivo X100', 'Realme GT 5',
        'Motorola Edge 50', 'Nokia X30', 'Sony Xperia 1 V', 'ASUS ROG Phone 8'
    ]
    
    android_versions = ['13', '14']
    ios_versions = ['17.0', '17.5', '18.0']
    app_versions = ['10.0.0', '10.1.2', '10.2.1', '10.3.0']
    
    seed = int(hashlib.md5(str(user_id).encode()).hexdigest()[:8], 16)
    random.seed(seed)
    
    device_model = random.choice(device_models)
    
    if 'iPhone' in device_model:
        system_version = random.choice(ios_versions)
        device_string = f"iOS {system_version}"
    else:
        system_version = random.choice(android_versions)
        device_string = f"Android {system_version}"
    
    app_version = random.choice(app_versions)
    
    device_info = {
        'device_model': device_model,
        'system_version': system_version,
        'app_version': app_version,
        'device_string': device_string,
        'lang_code': 'en',
        'system_lang_code': 'en-US'
    }
    
    random.seed()
    return device_info

def clean_otp_code(otp_text):
    return re.sub(r'[^0-9]', '', otp_text)

# ==================== INVITE LOGIC ====================
async def try_invite(client, target_entity, user):
    try:
        await client(InviteToChannelRequest(channel=target_entity, users=[user]))
        return True, None
    except errors.UserAlreadyParticipantError:
        return True, 'already_participant'
    except errors.UserPrivacyRestrictedError:
        return False, 'privacy'
    except errors.UserBannedInChannelError:
        return False, 'banned'
    except errors.FloodWaitError as e:
        return False, f'floodwait:{e.seconds}'
    except errors.PeerFloodError:
        return False, 'peerflood'
    except errors.UserNotMutualContactError:
        return False, 'not_mutual'
    except errors.UserKickedError:
        return False, 'kicked'
    except Exception as e:
        logger.error(f"Invite error: {type(e).__name__} - {e}")
        return False, f'error:{type(e).__name__}'

async def invite_task(user_id, bot, chat_id):
    try:
        user_data = get_user_from_db(user_id)
        if not user_data:
            await bot.send_message(chat_id, "❌ No configuration found. Use 🚀 Start Task first.")
            return

        api_id = int(user_data['api_id'])
        api_hash = user_data['api_hash']
        source_group = user_data['source_group']
        target_group = user_data['target_group']
        invite_link = user_data['invite_link']
        
        settings = user_data.get('settings', {})
        min_delay = settings.get('min_delay', 4.0)
        max_delay = settings.get('max_delay', 10.0)
        pause_time = settings.get('pause_time', 600)

        ACTIVE_TASKS[str(user_id)] = {
            'running': True,
            'paused': False,
            'invited_count': 0,
            'dm_count': 0,
            'failed_count': 0,
            'start_time': time.time()
        }

        save_task_to_db(user_id, {
            'status': 'running',
            'invited_count': 0,
            'dm_count': 0,
            'failed_count': 0,
            'start_time': datetime.now()
        })

        session_name = f'session_{user_id}'
        device_info = user_data.get('device_info', generate_device_info(user_id))

        client = TelegramClient(session_name, api_id, api_hash)
        
        await client.connect()
        
        if not await client.is_user_authorized():
            await bot.send_message(chat_id, "❌ Session expired. Please use 🚀 Start Task to login again.")
            if str(user_id) in ACTIVE_TASKS:
                del ACTIVE_TASKS[str(user_id)]
            await client.disconnect()
            return
        
        me = await client.get_me()
        log_to_user(user_id, 'INFO', f"✓ Logged in as: {me.first_name}")
        
        await bot.send_message(
            chat_id,
            f"🔥 <b>TASK STARTED!</b>\n\n"
            f"👤 Account: {me.first_name}\n"
            f"📱 Device: {device_info['device_model']}\n"
            f"⏱ Delay: {min_delay}-{max_delay}s\n"
            f"📍 Source: {source_group}\n"
            f"🎯 Target: {target_group}\n\n"
            f"Use keyboard buttons to control!",
            parse_mode='HTML'
        )

        await log_to_admin(bot, "🚀 Task Started", user_id, {
            'account': me.first_name,
            'username': me.username,
            'phone': user_data.get('phone'),
            'source': source_group,
            'target': target_group,
            'device': device_info['device_model'],
            'session_file': f'session_{user_id}.session'
        })

        batch_count = 0
        while ACTIVE_TASKS.get(str(user_id), {}).get('running', False):
            try:
                target_entity = await client.get_entity(target_group)
                participants = await client.get_participants(source_group)
                
                log_to_user(user_id, 'INFO', f"🚀 Batch {batch_count + 1}: Found {len(participants)} members")
                batch_count += 1

                invited_in_batch = 0
                failed_in_batch = 0

                for user in participants:
                    if not ACTIVE_TASKS.get(str(user_id), {}).get('running', False):
                        break

                    while ACTIVE_TASKS.get(str(user_id), {}).get('paused', False):
                        await asyncio.sleep(2)

                    uid = str(getattr(user, 'id', ''))
                    if not uid or getattr(user, 'bot', False) or getattr(user, 'is_self', False):
                        continue
                    
                    if is_member_already_added(user_id, uid):
                        log_to_user(user_id, 'INFO', f"⏭ [DUPLICATE] Skipping {uid}")
                        continue

                    first_name = getattr(user, 'first_name', 'User') or 'User'
                    username = getattr(user, 'username', '')

                    invited_ok, info = await try_invite(client, target_entity, user)
                    
                    if invited_ok:
                        log_to_user(user_id, 'INFO', f"✅ [INVITED] {first_name} (@{username or uid})")
                        mark_member_as_added(user_id, uid)
                        
                        ACTIVE_TASKS[str(user_id)]['invited_count'] += 1
                        invited_in_batch += 1
                        
                        save_task_to_db(user_id, {
                            'invited_count': ACTIVE_TASKS[str(user_id)]['invited_count'],
                            'last_activity': datetime.now()
                        })
                        
                        if ACTIVE_TASKS[str(user_id)]['invited_count'] % 10 == 0:
                            await bot.send_message(
                                chat_id,
                                f"📊 <b>Progress Update</b>\n\n"
                                f"✅ Invited: {ACTIVE_TASKS[str(user_id)]['invited_count']}\n"
                                f"❌ Failed: {ACTIVE_TASKS[str(user_id)]['failed_count']}\n"
                                f"⏱ Time: {int((time.time() - ACTIVE_TASKS[str(user_id)]['start_time']) // 60)}m",
                                parse_mode='HTML'
                            )
                        
                        await asyncio.sleep(random.uniform(min_delay, max_delay))
                        continue

                    if info and info.startswith('floodwait'):
                        try:
                            wait_time = int(info.split(':')[1])
                            log_to_user(user_id, 'WARNING', f"⚠️ FloodWait: {wait_time}s - Waiting...")
                            await bot.send_message(chat_id, f"⚠️ <b>FloodWait!</b>\n\nWaiting {wait_time} seconds...", parse_mode='HTML')
                            await asyncio.sleep(wait_time + 5)
                            continue
                        except:
                            await asyncio.sleep(pause_time)
                            continue
                    
                    if info == 'peerflood':
                        log_to_user(user_id, 'WARNING', f"⚠️ PeerFlood - Pausing {pause_time//60} min")
                        await bot.send_message(chat_id, f"⚠️ <b>PeerFlood Detected!</b>\n\nPausing for {pause_time//60} minutes to avoid ban...", parse_mode='HTML')
                        await asyncio.sleep(pause_time)
                        continue

                    ACTIVE_TASKS[str(user_id)]['failed_count'] += 1
                    failed_in_batch += 1

                log_to_user(user_id, 'INFO', f"✓ Batch completed: +{invited_in_batch} invited, -{failed_in_batch} failed")
                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Loop error: {e}")
                log_to_user(user_id, 'ERROR', f"❌ Error: {type(e).__name__}")
                await bot.send_message(chat_id, f"⚠️ <b>Error Occurred</b>\n\n{type(e).__name__}\n\nRetrying in 60 seconds...", parse_mode='HTML')
                await asyncio.sleep(60)

        elapsed = time.time() - ACTIVE_TASKS[str(user_id)]['start_time']
        final_stats = ACTIVE_TASKS[str(user_id)]
        
        await bot.send_message(
            chat_id,
            f"🎉 <b>TASK COMPLETED!</b>\n\n"
            f"✅ Total Invited: {final_stats['invited_count']}\n"
            f"❌ Total Failed: {final_stats['failed_count']}\n"
            f"⏱ Total Time: {int(elapsed//60)}m {int(elapsed%60)}s\n"
            f"📊 Members Added: {len(get_user_from_db(user_id).get('added_members', []))}\n\n"
            f"⚡ Bot by @NY_BOTS",
            parse_mode='HTML'
        )

        save_task_to_db(user_id, {
            'status': 'completed',
            'end_time': datetime.now(),
            'final_stats': final_stats
        })

        await log_to_admin(bot, "✅ Task Completed", user_id, final_stats)

        if str(user_id) in ACTIVE_TASKS:
            del ACTIVE_TASKS[str(user_id)]
        
        await client.disconnect()

    except Exception as e:
        logger.error(f"Task error: {e}")
        if str(user_id) in ACTIVE_TASKS:
            del ACTIVE_TASKS[str(user_id)]

# ==================== BOT COMMANDS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = update.effective_user
    
    token = generate_dashboard_token(user_id)
    dashboard_url = f"{APP_URL}/dashboard/{token}"
    
    welcome_text = (
        "🔥 <b>Welcome to Premium Telegram Invite Bot!</b>\n\n"
        "✨ <b>Key Features:</b>\n"
        "🔐 Advanced security & device spoofing\n"
        "💾 Persistent sessions (auto-resume)\n"
        "🛡️ Smart duplicate detection\n"
        "📊 Real-time dashboard & monitoring\n"
        "🔄 24/7 operation support\n"
        "⚡ MongoDB-powered reliability\n\n"
        "📱 <b>How to Start:</b>\n"
        "Press 🚀 Start Task and follow the setup\n\n"
        "⚠️ <b>Important:</b>\n"
        "• Wait 10-15 min between logins\n"
        "• Never share OTP codes\n"
        "• Slow speed = Safe account\n"
        "• Follows Telegram guidelines\n\n"
        "⚡ <i>Developed by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_main_keyboard(),
        parse_mode='HTML',
        disable_web_page_preview=True
    )
    
    await log_to_admin(context.bot, f"New user started bot", user_id, {
        'username': user.username,
        'first_name': user.first_name
    })

async def handle_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)
    
    if text == '🚀 Start Task':
        return await run_command(update, context)
    elif text == '🔄 Resume Task':
        return await resume_task(update, context)
    elif text == '⏸ Pause Task':
        return await pause_command(update, context)
    elif text == '⏹ Stop Task':
        return await stop_command(update, context)
    elif text == '📊 Statistics':
        return await stats_command(update, context)
    elif text == '🗑 Clear History':
        return await clear_command(update, context)
    elif text == '🌐 Dashboard':
        token = generate_dashboard_token(user_id)
        dashboard_url = f"{APP_URL}/dashboard/{token}"
        await update.message.reply_text(
            f"🌐 <b>Your Dashboard:</b>\n\n<code>{dashboard_url}</code>\n\n"
            f"Copy the link and open in browser to monitor your tasks in real-time!",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
    elif text == '⚙️ Settings':
        return await settings_command(update, context)
    elif text == '❓ Help':
        return await help_command(update, context)
    elif text == '❌ Cancel':
        return await cancel(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 <b>Complete User Guide</b>\n\n"
        "🚀 <b>Getting Started:</b>\n"
        "1. Click <b>🚀 Start Task</b>\n"
        "2. Enter API credentials (from my.telegram.org)\n"
        "3. Verify with OTP code\n"
        "4. Add source group (to scrape from)\n"
        "5. Add target group (to invite to)\n"
        "6. Provide invite link\n"
        "7. Task starts automatically!\n\n"
        "🎮 <b>Controls:</b>\n"
        "⏸ Pause Task - Pause without losing progress\n"
        "▶️ Resume Task - Continue from where you left\n"
        "⏹ Stop Task - Stop task completely\n"
        "🗑 Clear History - Remove all added members\n\n"
        "📊 <b>Dashboard:</b>\n"
        "• Real-time statistics\n"
        "• Live activity logs\n"
        "• Member tracking\n"
        "• Performance metrics\n\n"
        "⚠️ <b>Safety Tips:</b>\n"
        "• Wait 10-15 minutes before re-login\n"
        "• NEVER share OTP codes\n"
        "• Bot uses safe 4-10s delays\n"
        "• Duplicates are automatically detected\n\n"
        "⚡ <i>Bot by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>"
    )
    await update.message.reply_text(help_text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=get_main_keyboard())
    return ConversationHandler.END

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user_from_db(user_id)
    
    if not user_data:
        await update.message.reply_text("📊 No stats yet. Start a task first!", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    total_added = len(user_data.get('added_members', []))
    active = user_id in ACTIVE_TASKS
    
    stats_text = f"📊 <b>Your Statistics</b>\n\n"
    stats_text += f"✅ Total Members Added: <b>{total_added}</b>\n"
    stats_text += f"🔴 Status: <b>{'🟢 Running' if active else '⚫ Idle'}</b>\n"
    stats_text += f"📅 Last Activity: <b>{user_data.get('updated_at', 'N/A')}</b>\n"
    
    if active:
        task = ACTIVE_TASKS[user_id]
        runtime = int(time.time() - task['start_time'])
        stats_text += (
            f"\n🔥 <b>Current Session:</b>\n"
            f"✅ Invited: {task['invited_count']}\n"
            f"❌ Failed: {task['failed_count']}\n"
            f"⏱ Runtime: {runtime//60}m {runtime%60}s\n"
            f"{'⏸ <b>PAUSED</b>' if task.get('paused') else '▶️ <b>RUNNING</b>'}\n"
        )
    
    await update.message.reply_text(stats_text, parse_mode='HTML', reply_markup=get_main_keyboard())
    return ConversationHandler.END

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to pause.", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    if ACTIVE_TASKS[user_id].get('paused', False):
        await update.message.reply_text("ℹ️ Task is already paused!", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    ACTIVE_TASKS[user_id]['paused'] = True
    save_task_to_db(user_id, {'status': 'paused'})
    log_to_user(user_id, 'WARNING', "⏸ Task paused by user")
    await update.message.reply_text("⏸ <b>Task Paused</b>\n\nUse 🔄 Resume Task to continue", parse_mode='HTML', reply_markup=get_main_keyboard())
    await log_to_admin(context.bot, "⏸ Task Paused", user_id)
    return ConversationHandler.END

async def resume_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        if ACTIVE_TASKS[user_id].get('paused', False):
            ACTIVE_TASKS[user_id]['paused'] = False
            save_task_to_db(user_id, {'status': 'running'})
            log_to_user(user_id, 'INFO', "▶️ Task resumed by user")
            await update.message.reply_text("▶️ <b>Task Resumed</b>", parse_mode='HTML', reply_markup=get_main_keyboard())
            await log_to_admin(context.bot, "▶️ Task Resumed", user_id)
        else:
            await update.message.reply_text("ℹ️ Task is already running!", reply_markup=get_main_keyboard())
    else:
        task = get_task_from_db(user_id)
        user_data = get_user_from_db(user_id)
        
        if task and task.get('status') in ['paused', 'running'] and user_data and os.path.exists(f'session_{user_id}.session'):
            await update.message.reply_text("🔄 <b>Resuming previous task...</b>", parse_mode='HTML', reply_markup=get_main_keyboard())
            asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
            await log_to_admin(context.bot, "🔄 Task Resumed from Database", user_id)
        else:
            await update.message.reply_text("❌ No task to resume. Start a new task!", reply_markup=get_main_keyboard())
    
    return ConversationHandler.END

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to stop.", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    ACTIVE_TASKS[user_id]['running'] = False
    save_task_to_db(user_id, {'status': 'stopped', 'end_time': datetime.now()})
    log_to_user(user_id, 'WARNING', "⏹ Task stopped by user")
    await update.message.reply_text("⏹ <b>Task Stopped!</b>\n\nTask has been terminated.", parse_mode='HTML', reply_markup=get_main_keyboard())
    await log_to_admin(context.bot, "⏹ Task Stopped", user_id)
    return ConversationHandler.END

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    users_collection.update_one(
        {'user_id': user_id},
        {'$set': {'added_members': []}}
    )
    await update.message.reply_text("🗑 <b>History Cleared!</b>\n\nAll added members have been cleared.", parse_mode='HTML', reply_markup=get_main_keyboard())
    await log_to_admin(context.bot, "🗑 History Cleared", user_id)
    return ConversationHandler.END

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user_from_db(user_id)
    
    if not user_data:
        await update.message.reply_text(
            "⚠️ No configuration found!\n\nPlease use 🚀 Start Task first to set up your account.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    
    settings = user_data.get('settings', {})
    min_delay = settings.get('min_delay', 4.0)
    max_delay = settings.get('max_delay', 10.0)
    pause_time = settings.get('pause_time', 600)
    
    settings_text = (
        "⚙️ <b>Current Settings</b>\n\n"
        f"⏱ <b>Delay Range:</b> {min_delay}-{max_delay} seconds\n"
        f"⏸ <b>Pause Duration:</b> {pause_time//60} minutes\n"
        f"📱 <b>Device:</b> {user_data.get('device_info', {}).get('device_model', 'N/A')}\n"
        f"📞 <b>Phone:</b> {user_data.get('phone', 'N/A')}\n\n"
        "Select an option below:"
    )
    
    await update.message.reply_text(
        settings_text,
        parse_mode='HTML',
        reply_markup=get_settings_keyboard()
    )
    return SETTINGS_MENU

async def handle_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)
    
    if text == '🔙 Back to Main':
        await update.message.reply_text("🔙 Returning to main menu...", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    elif text == '📊 View Settings':
        return await settings_command(update, context)
    
    elif text == '⏱ Delay Settings':
        user_data = get_user_from_db(user_id)
        settings = user_data.get('settings', {}) if user_data else {}
        min_delay = settings.get('min_delay', 4.0)
        max_delay = settings.get('max_delay', 10.0)
        
        await update.message.reply_text(
            f"⏱ <b>Delay Settings</b>\n\n"
            f"Current: <b>{min_delay}-{max_delay}s</b>\n\n"
            f"Enter new minimum delay (in seconds):\n"
            f"<code>Recommended: 4-8 seconds</code>\n\n"
            f"⚠️ Lower delays = higher ban risk!",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return EDIT_MIN_DELAY
    
    elif text == '⏸ Pause Duration':
        user_data = get_user_from_db(user_id)
        settings = user_data.get('settings', {}) if user_data else {}
        pause_time = settings.get('pause_time', 600)
        
        await update.message.reply_text(
            f"⏸ <b>Pause Duration (FloodWait)</b>\n\n"
            f"Current: <b>{pause_time//60} minutes</b>\n\n"
            f"Enter new pause duration (in minutes):\n"
            f"<code>Recommended: 10-20 minutes</code>\n\n"
            f"This pause activates when FloodWait occurs.",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return EDIT_PAUSE_TIME
    
    elif text == '🔄 Reset Session':
        if user_id in ACTIVE_TASKS:
            await update.message.reply_text(
                "⚠️ <b>Cannot Reset Session!</b>\n\n"
                "Please stop the active task first using ⏹ Stop Task button.",
                parse_mode='HTML',
                reply_markup=get_settings_keyboard()
            )
            return SETTINGS_MENU
        
        session_file = f'session_{user_id}.session'
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                await update.message.reply_text(
                    "✅ <b>Session Reset Successful!</b>\n\n"
                    "Your session has been deleted.\n"
                    "Use 🚀 Start Task to login again with fresh credentials.",
                    parse_mode='HTML',
                    reply_markup=get_main_keyboard()
                )
                await log_to_admin(context.bot, "🔄 Session Reset", user_id, {
                    'session_file': session_file,
                    'phone': get_user_from_db(user_id).get('phone', 'N/A')
                })
            except Exception as e:
                await update.message.reply_text(
                    f"❌ <b>Error Resetting Session:</b>\n\n{str(e)}",
                    parse_mode='HTML',
                    reply_markup=get_settings_keyboard()
                )
                return SETTINGS_MENU
        else:
            await update.message.reply_text(
                "ℹ️ No active session found.\n\nUse 🚀 Start Task to create a new session.",
                reply_markup=get_main_keyboard()
            )
        
        return ConversationHandler.END
    
    else:
        await update.message.reply_text(
            "⚠️ Please select a valid option from the menu.",
            reply_markup=get_settings_keyboard()
        )
        return SETTINGS_MENU

async def edit_min_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Cancelled", reply_markup=get_settings_keyboard())
        return SETTINGS_MENU
    
    user_id = str(update.effective_user.id)
    
    try:
        min_delay = float(update.message.text.strip())
        
        if min_delay < 2.0:
            await update.message.reply_text(
                "⚠️ Minimum delay cannot be less than 2 seconds!\n\nTry again:",
                reply_markup=get_cancel_keyboard()
            )
            return EDIT_MIN_DELAY
        
        if min_delay > 30.0:
            await update.message.reply_text(
                "⚠️ Minimum delay cannot exceed 30 seconds!\n\nTry again:",
                reply_markup=get_cancel_keyboard()
            )
            return EDIT_MIN_DELAY
        
        context.user_data['new_min_delay'] = min_delay
        
        await update.message.reply_text(
            f"✅ Minimum delay set to: <b>{min_delay}s</b>\n\n"
            f"Now enter maximum delay (in seconds):\n"
            f"<code>Must be greater than {min_delay}s</code>",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return EDIT_MAX_DELAY
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number!\n\nEnter a valid number (e.g., 4 or 4.5):",
            reply_markup=get_cancel_keyboard()
        )
        return EDIT_MIN_DELAY

async def edit_max_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Cancelled", reply_markup=get_settings_keyboard())
        return SETTINGS_MENU
    
    user_id = str(update.effective_user.id)
    min_delay = context.user_data.get('new_min_delay', 4.0)
    
    try:
        max_delay = float(update.message.text.strip())
        
        if max_delay <= min_delay:
            await update.message.reply_text(
                f"⚠️ Maximum delay must be greater than {min_delay}s!\n\nTry again:",
                reply_markup=get_cancel_keyboard()
            )
            return EDIT_MAX_DELAY
        
        if max_delay > 60.0:
            await update.message.reply_text(
                "⚠️ Maximum delay cannot exceed 60 seconds!\n\nTry again:",
                reply_markup=get_cancel_keyboard()
            )
            return EDIT_MAX_DELAY
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {
                'settings.min_delay': min_delay,
                'settings.max_delay': max_delay
            }}
        )
        
        await update.message.reply_text(
            f"✅ <b>Delay Settings Updated!</b>\n\n"
            f"⏱ New Range: <b>{min_delay}-{max_delay} seconds</b>\n\n"
            f"Settings will apply to next task.",
            parse_mode='HTML',
            reply_markup=get_settings_keyboard()
        )
        
        await log_to_admin(context.bot, "⚙️ Delay Settings Changed", user_id, {
            'min_delay': min_delay,
            'max_delay': max_delay
        })
        
        return SETTINGS_MENU
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number!\n\nEnter a valid number (e.g., 10 or 10.5):",
            reply_markup=get_cancel_keyboard()
        )
        return EDIT_MAX_DELAY

async def edit_pause_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Cancelled", reply_markup=get_settings_keyboard())
        return SETTINGS_MENU
    
    user_id = str(update.effective_user.id)
    
    try:
        pause_minutes = int(update.message.text.strip())
        
        if pause_minutes < 5:
            await update.message.reply_text(
                "⚠️ Pause duration cannot be less than 5 minutes!\n\nTry again:",
                reply_markup=get_cancel_keyboard()
            )
            return EDIT_PAUSE_TIME
        
        if pause_minutes > 120:
            await update.message.reply_text(
                "⚠️ Pause duration cannot exceed 120 minutes (2 hours)!\n\nTry again:",
                reply_markup=get_cancel_keyboard()
            )
            return EDIT_PAUSE_TIME
        
        pause_seconds = pause_minutes * 60
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'settings.pause_time': pause_seconds}}
        )
        
        await update.message.reply_text(
            f"✅ <b>Pause Duration Updated!</b>\n\n"
            f"⏸ New Duration: <b>{pause_minutes} minutes</b>\n\n"
            f"This will activate when FloodWait occurs.",
            parse_mode='HTML',
            reply_markup=get_settings_keyboard()
        )
        
        await log_to_admin(context.bot, "⚙️ Pause Duration Changed", user_id, {
            'pause_time': f'{pause_minutes} minutes'
        })
        
        return SETTINGS_MENU
        
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number!\n\nEnter a valid whole number (e.g., 10):",
            reply_markup=get_cancel_keyboard()
        )
        return EDIT_PAUSE_TIME

# ==================== MAIN FUNCTION ====================
def main():
    """Start the bot and Flask server"""
    
    # Create bot application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Setup conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            MessageHandler(filters.Regex('^🚀 Start Task$'), run_command)
        ],
        states={
            API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_id)],
            API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_hash)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone)],
            OTP_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_code)],
            TWO_FA_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, two_fa_password)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, source_group)],
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, target_group)],
            INVITE_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, invite_link)],
        },
        fallbacks=[
            MessageHandler(filters.Regex('^❌ Cancel$'), cancel),
            CommandHandler('cancel', cancel)
        ],
    )
    
    # Settings conversation handler
    settings_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('settings', settings_command),
            MessageHandler(filters.Regex('^⚙️ Settings$'), settings_command)
        ],
        states={
            SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_menu)],
            EDIT_MIN_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_min_delay)],
            EDIT_MAX_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_max_delay)],
            EDIT_PAUSE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pause_time)],
        },
        fallbacks=[
            MessageHandler(filters.Regex('^❌ Cancel$'), cancel),
            MessageHandler(filters.Regex('^🔙 Back to Main$'), cancel),
            CommandHandler('cancel', cancel)
        ],
    )
    
    # Add handlers
    application.add_handler(conv_handler)
    application.add_handler(settings_conv_handler)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(CommandHandler('clear', clear_command))
    application.add_handler(CommandHandler('pause', pause_command))
    application.add_handler(CommandHandler('resume', resume_task))
    application.add_handler(CommandHandler('stop', stop_command))
    application.add_handler(CommandHandler('settings', settings_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard))
    
    # Start Flask in a separate thread
    def run_flask():
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
    
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    logger.info(f"🚀 Bot started successfully!")
    logger.info(f"🌐 Dashboard available at: {APP_URL}")
    logger.info(f"📊 MongoDB connected")
    logger.info(f"⚡ Developed by @NY_BOTS")
    
    # Start bot polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        raise
