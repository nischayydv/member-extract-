import asyncio
import os
import json
import random
import time
import logging
import hashlib
import re
import secrets
from datetime import datetime
from threading import Thread
from queue import Queue
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, ConversationHandler, 
    MessageHandler, filters, ContextTypes
)
from telegram.constants import MessageEffectId
from flask import Flask, render_template_string, jsonify, Response, request, abort
from pymongo import MongoClient, errors as mongo_errors
import queue

# ---------------- CONFIGURATION ----------------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')
ADMIN_LOG_CHANNEL = os.environ.get('ADMIN_LOG_CHANNEL', '-1001234567890')  # Admin channel ID
PORT = int(os.environ.get('PORT', 10000))
APP_URL = os.environ.get('APP_URL', 'https://your-app.onrender.com')

# ---------------- MONGODB SETUP ----------------
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    db = mongo_client['telegram_invite_bot']
    users_collection = db['users']
    stats_collection = db['stats']
    sessions_collection = db['sessions']
    tasks_collection = db['tasks']
    logs_collection = db['logs']
    print("✅ MongoDB connected successfully!")
except Exception as e:
    print(f"❌ MongoDB connection failed: {e}")
    exit(1)

# ---------------- FLASK APP SETUP ----------------
app = Flask(__name__)
LOG_QUEUE = Queue(maxsize=1000)
USER_LOG_QUEUES = {}
DASHBOARD_TOKENS = {}

# ---------------- LOGGING SETUP ----------------
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

# ---------------- STATES ----------------
API_ID, API_HASH, PHONE, OTP_CODE, TWO_FA_PASSWORD, SOURCE, TARGET, INVITE_LINK = range(8)

# ---------------- ACTIVE TASKS ----------------
ACTIVE_TASKS = {}
TEMP_CLIENTS = {}

# ---------------- KEYBOARD LAYOUTS ----------------
def get_main_keyboard():
    keyboard = [
        [KeyboardButton('🚀 Start Task'), KeyboardButton('🔄 Resume Task')],
        [KeyboardButton('⏸ Pause Task'), KeyboardButton('⏹ Stop Task')],
        [KeyboardButton('📊 Statistics'), KeyboardButton('🗑 Clear History')],
        [KeyboardButton('🌐 Dashboard'), KeyboardButton('⚙️ Settings')],
        [KeyboardButton('❓ Help')]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_cancel_keyboard():
    keyboard = [[KeyboardButton('❌ Cancel')]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ---------------- HELPER FUNCTIONS ----------------
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
    """Send logs to admin channel"""
    try:
        log_text = f"📊 <b>Bot Activity Log</b>\n\n"
        log_text += f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        
        if user_id:
            log_text += f"👤 User ID: <code>{user_id}</code>\n"
        
        log_text += f"📝 Message: {message}\n"
        
        if data:
            log_text += f"\n📦 <b>Data:</b>\n<pre>{json.dumps(data, indent=2, ensure_ascii=False)}</pre>"
        
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

def get_user_from_db(user_id):
    return users_collection.find_one({'user_id': str(user_id)})

def save_user_to_db(user_id, data):
    users_collection.update_one(
        {'user_id': str(user_id)},
        {'$set': {**data, 'updated_at': datetime.now()}},
        upsert=True
    )

def get_task_from_db(user_id):
    return tasks_collection.find_one({'user_id': str(user_id), 'status': 'running'})

def save_task_to_db(user_id, task_data):
    tasks_collection.update_one(
        {'user_id': str(user_id)},
        {'$set': {**task_data, 'updated_at': datetime.now()}},
        upsert=True
    )

def is_member_already_added(user_id, member_id):
    """Check if member is already added"""
    user_data = get_user_from_db(user_id)
    if user_data and 'added_members' in user_data:
        return str(member_id) in user_data['added_members']
    return False

def mark_member_as_added(user_id, member_id):
    """Mark member as added"""
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

# ---------------- INVITE LOGIC ----------------
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

        # Initialize task tracking
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
            f"✅ <b>Task Started!</b>\n\n"
            f"👤 Account: {me.first_name}\n"
            f"📱 Device: {device_info['device_model']}\n"
            f"⏱ Delay: {min_delay}-{max_delay}s\n\n"
            f"Use keyboard buttons to control!",
            parse_mode='HTML',
            message_effect_id=MessageEffectId.FIRE
        )

        await log_to_admin(bot, "Task Started", user_id, {
            'account': me.first_name,
            'username': me.username,
            'source': source_group,
            'target': target_group,
            'device': device_info['device_model']
        })

        while ACTIVE_TASKS.get(str(user_id), {}).get('running', False):
            try:
                target_entity = await client.get_entity(target_group)
                participants = await client.get_participants(source_group)
                
                log_to_user(user_id, 'INFO', f"🚀 Found {len(participants)} members")

                for user in participants:
                    if not ACTIVE_TASKS.get(str(user_id), {}).get('running', False):
                        break

                    while ACTIVE_TASKS.get(str(user_id), {}).get('paused', False):
                        await asyncio.sleep(2)

                    uid = str(getattr(user, 'id', ''))
                    if not uid or getattr(user, 'bot', False) or getattr(user, 'is_self', False):
                        continue
                    
                    # Check if already added
                    if is_member_already_added(user_id, uid):
                        continue

                    first_name = getattr(user, 'first_name', 'User') or 'User'
                    username = getattr(user, 'username', '')

                    invited_ok, info = await try_invite(client, target_entity, user)
                    
                    if invited_ok:
                        log_to_user(user_id, 'INFO', f"✅ [INVITED] {first_name}")
                        mark_member_as_added(user_id, uid)
                        
                        ACTIVE_TASKS[str(user_id)]['invited_count'] += 1
                        save_task_to_db(user_id, {
                            'invited_count': ACTIVE_TASKS[str(user_id)]['invited_count'],
                            'last_activity': datetime.now()
                        })
                        
                        if ACTIVE_TASKS[str(user_id)]['invited_count'] % 10 == 0:
                            await bot.send_message(
                                chat_id,
                                f"📊 Progress: {ACTIVE_TASKS[str(user_id)]['invited_count']} invited"
                            )
                        
                        await asyncio.sleep(random.uniform(min_delay, max_delay))
                        continue

                    if info and info.startswith('floodwait'):
                        wait_time = int(info.split(':')[1])
                        log_to_user(user_id, 'WARNING', f"⚠️ FloodWait: {wait_time}s")
                        await bot.send_message(chat_id, f"⚠️ FloodWait! Waiting {wait_time}s...")
                        await asyncio.sleep(wait_time + 5)
                        continue
                    
                    if info == 'peerflood':
                        log_to_user(user_id, 'WARNING', f"⚠️ PeerFlood - Pausing")
                        await bot.send_message(chat_id, f"⚠️ PeerFlood! Pausing {pause_time//60} minutes...")
                        await asyncio.sleep(pause_time)
                        continue

                    ACTIVE_TASKS[str(user_id)]['failed_count'] += 1

                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Loop error: {e}")
                await bot.send_message(chat_id, f"❌ Error: {type(e).__name__}\nRetrying...")
                await asyncio.sleep(60)

        # Task completed
        elapsed = time.time() - ACTIVE_TASKS[str(user_id)]['start_time']
        final_stats = ACTIVE_TASKS[str(user_id)]
        
        await bot.send_message(
            chat_id,
            f"🎉 <b>Task Completed!</b>\n\n"
            f"✅ Invited: {final_stats['invited_count']}\n"
            f"❌ Failed: {final_stats['failed_count']}\n"
            f"⏱ Time: {int(elapsed//60)}m {int(elapsed%60)}s",
            parse_mode='HTML'
        )

        save_task_to_db(user_id, {
            'status': 'completed',
            'end_time': datetime.now(),
            'final_stats': final_stats
        })

        if str(user_id) in ACTIVE_TASKS:
            del ACTIVE_TASKS[str(user_id)]
        
        await client.disconnect()

    except Exception as e:
        logger.error(f"Task error: {e}")
        if str(user_id) in ACTIVE_TASKS:
            del ACTIVE_TASKS[str(user_id)]

# ---------------- BOT COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    token = generate_dashboard_token(user_id)
    dashboard_url = f"{APP_URL}/dashboard/{token}"
    
    welcome_text = (
        "🔥 <b>Welcome to Premium Telegram Invite Bot!</b>\n\n"
        "✨ <b>Features:</b>\n"
        "• 🔐 Advanced security & device spoofing\n"
        "• 💾 Persistent sessions (auto-resume)\n"
        "• 🛡️ Smart duplicate detection\n"
        "• 📊 Real-time dashboard\n"
        "• 🔄 24/7 operation support\n\n"
        "⚡ <i>Developed by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_main_keyboard(),
        parse_mode='HTML',
        disable_web_page_preview=True,
        message_effect_id=MessageEffectId.FIRE
    )
    
    await log_to_admin(context.bot, "New user started bot", user_id)

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
        await update.message.reply_text(
            "✅ <b>OTP Sent!</b>\n\n📱 <b>Step 4/7: Verification</b>\n\nEnter 5-digit code:\n\n⚠️ NEVER share with anyone!",
            parse_mode='HTML'
        )
        return OTP_CODE
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {e}\n\nTry again or wait 10-15 min.", parse_mode='HTML')
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
        await context.bot.send_message(update.effective_chat.id, f"❌ Invalid OTP. Must be 5 digits.\n\nTry again:")
        return OTP_CODE
    
    if user_id not in TEMP_CLIENTS:
        await context.bot.send_message(update.effective_chat.id, "❌ Session expired. Use 🚀 Start Task.")
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    phone_number = context.user_data['phone']
    
    try:
        await context.bot.send_message(update.effective_chat.id, "🔐 Verifying...")
        
        await client.sign_in(
            phone=phone_number,
            code=otp,
            phone_code_hash=TEMP_CLIENTS[user_id]['phone_hash']
        )
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ Login: {me.first_name}")
            
            await client.disconnect()
            
            device_info = TEMP_CLIENTS[user_id]['device_info']
            
            await log_to_admin(context.bot, "Login successful", user_id, {
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
                f"🎉 <b>Success!</b>\n\n👤 {me.first_name}\n📱 @{me.username or 'N/A'}\n\n"
                f"📍 <b>Step 5/7: Source Group</b>\n\nEnter source group:\n• @groupname\n• https://t.me/group",
                parse_mode='HTML'
            )
            
            return SOURCE
        else:
            await context.bot.send_message(update.effective_chat.id, "❌ Login failed. Try again.")
            if user_id in TEMP_CLIENTS:
                try:
                    await TEMP_CLIENTS[user_id]['client'].disconnect()
                except:
                    pass
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except errors.SessionPasswordNeededError:
        logger.info(f"🔐 2FA for {user_id}")
        await context.bot.send_message(
            update.effective_chat.id,
            "🔐 <b>Step 4.5/7: 2FA</b>\n\nEnter your 2FA password:",
            parse_mode='HTML'
        )
        return TWO_FA_PASSWORD
    except Exception as e:
        logger.error(f"OTP error: {e}")
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Error: {str(e)}\n\nPossible issues:\n• Wrong OTP\n• Expired code\n• Security block\n\nUse 🚀 Start Task to retry.",
            parse_mode='HTML'
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
        await context.bot.send_message(update.effective_chat.id, "❌ Session expired.")
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    
    try:
        await context.bot.send_message(update.effective_chat.id, "🔐 Verifying 2FA...")
        
        await client.sign_in(password=password)
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ 2FA login: {me.first_name}")
            
            await client.disconnect()
            
            device_info = TEMP_CLIENTS[user_id]['device_info']
            
            await log_to_admin(context.bot, "2FA login successful", user_id, {
                'account_name': me.first_name,
                'username': me.username,
                'phone': context.user_data['phone'],
                'device': device_info['device_model']
            })
            
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            await context.bot.send_message(
                update.effective_chat.id,
                f"🎉 <b>Success!</b>\n\n👤 {me.first_name}\n\n"
                f"📍 <b>Step 5/7: Source Group</b>\n\nEnter source group:",
                parse_mode='HTML'
            )
            
            return SOURCE
        else:
            await context.bot.send_message(update.effective_chat.id, "❌ 2FA failed.")
            if user_id in TEMP_CLIENTS:
                try:
                    await TEMP_CLIENTS[user_id]['client'].disconnect()
                except:
                    pass
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"2FA error: {e}")
        await context.bot.send_message(update.effective_chat.id, f"❌ Wrong 2FA password.")
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
        "✅ Source saved!\n\n🎯 <b>Step 6/7: Target Group</b>\n\nEnter target group:",
        parse_mode='HTML'
    )
    return TARGET

async def target_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    context.user_data['target_group'] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Target saved!\n\n🔗 <b>Step 7/7: Invite Link</b>\n\nEnter invite link:\nExample: https://t.me/+AbCdEf",
        parse_mode='HTML'
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
    
    await log_to_admin(context.bot, "Setup complete - Starting task", user_id, user_data)
    
    await update.message.reply_text(
        "🎉 <b>Setup Complete!</b>\n\n"
        "✅ Configuration saved\n"
        "🚀 Starting task now...\n\n"
        f"🌐 Dashboard: {dashboard_url}\n\n"
        "💡 Control via keyboard buttons!",
        parse_mode='HTML',
        reply_markup=get_main_keyboard(),
        message_effect_id=MessageEffectId.FIRE
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
        "❌ <b>Cancelled</b>\n\nUse 🚀 Start Task anytime!",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

# ---------------- FLASK ROUTES ----------------
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
            .emoji { font-size: 3em; margin-bottom: 20px; }
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
                        document.getElementById('active-tasks')._text(
            f"🌐 <b>Your Dashboard:</b>\n\n{APP_URL}/dashboard/{token}",
            parse_mode='HTML'
        )
    elif text == '⚙️ Settings':
        await update.message.reply_text("⚙️ Settings coming soon!")
    elif text == '❓ Help':
        return await help_command(update, context)
    elif text == '❌ Cancel':
        return await cancel(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 <b>How to Use:</b>\n\n"
        "1️⃣ Click <b>🚀 Start Task</b>\n"
        "2️⃣ Enter your API credentials\n"
        "3️⃣ Verify with OTP\n"
        "4️⃣ Add source & target groups\n"
        "5️⃣ Task starts automatically!\n\n"
        "⏸ Use buttons to pause/resume/stop\n"
        "📊 View real-time stats\n"
        "🌐 Monitor via dashboard\n\n"
        "⚡ <i>Bot by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>"
    )
    await update.message.reply_text(help_text, parse_mode='HTML', disable_web_page_preview=True)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user_from_db(user_id)
    
    if not user_data:
        await update.message.reply_text("📊 No stats yet. Start a task first!")
        return
    
    total_added = len(user_data.get('added_members', []))
    active = user_id in ACTIVE_TASKS
    
    stats_text = f"📊 <b>Your Statistics</b>\n\n"
    stats_text += f"✅ Total Members Added: <b>{total_added}</b>\n"
    stats_text += f"🔴 Status: <b>{'🟢 Running' if active else '⚫ Idle'}</b>\n"
    
    if active:
        task = ACTIVE_TASKS[user_id]
        runtime = int(time.time() - task['start_time'])
        stats_text += (
            f"\n🔥 <b>Current Session:</b>\n"
            f"✅ Invited: {task['invited_count']}\n"
            f"❌ Failed: {task['failed_count']}\n"
            f"⏱ Runtime: {runtime//60}m {runtime%60}s\n"
        )
    
    await update.message.reply_text(stats_text, parse_mode='HTML')

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to pause.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = True
    save_task_to_db(user_id, {'status': 'paused'})
    await update.message.reply_text("⏸ <b>Task Paused</b>", parse_mode='HTML')

async def resume_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        ACTIVE_TASKS[user_id]['paused'] = False
        save_task_to_db(user_id, {'status': 'running'})
        await update.message.reply_text("▶️ <b>Task Resumed</b>", parse_mode='HTML')
    else:
        # Resume from database
        task = get_task_from_db(user_id)
        if task and task.get('status') in ['paused', 'running']:
            await update.message.reply_text("🔄 Resuming previous task...")
            asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
        else:
            await update.message.reply_text("❌ No task to resume. Start new task!")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task.")
        return
    
    ACTIVE_TASKS[user_id]['running'] = False
    save_task_to_db(user_id, {'status': 'stopped'})
    await update.message.reply_text("⏹ <b>Task Stopping...</b>", parse_mode='HTML')

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    users_collection.update_one(
        {'user_id': user_id},
        {'$set': {'added_members': []}}
    )
    await update.message.reply_text("🗑 <b>History cleared!</b>", parse_mode='HTML')

# ---------------- CONVERSATION HANDLERS ----------------
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("❌ Task already running!")
        return ConversationHandler.END
    
    user_data = get_user_from_db(user_id)
    if user_data and os.path.exists(f'session_{user_id}.session'):
        await update.message.reply_text("🔍 Checking saved session...")
        asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
        return ConversationHandler.END
    
    await update.message.reply_text(
        "🔐 <b>Step 1/7: API ID</b>\n\nGet from: https://my.telegram.org\n\nEnter API_ID:",
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
        await update.message.reply_text("✅ API ID saved!\n\n🔑 <b>Step 2/7: API Hash</b>\n\nEnter API_HASH:", parse_mode='HTML')
        return API_HASH
    except ValueError:
        await update.message.reply_text("❌ Invalid. Must be numbers only.")
        return API_ID

async def api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    context.user_data['api_hash'] = update.message.text.strip()
    await update.message.reply_text("✅ Saved!\n\n📱 <b>Step 3/7: Phone</b>\n\nFormat: +1234567890", parse_mode='HTML')
    return PHONE

async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    phone_number = update.message.text.strip()
    if not phone_number.startswith('+'):
        await update.message.reply_text("❌ Must start with +")
        return PHONE
    
    context.user_data['phone'] = phone_number
    user_id = str(update.effective_user.id)
    
    api_id = int(context.user_data['api_id'])
    api_hash = context.user_data['api_hash']
    
    device_info = generate_device_info(user_id)
    
    try:
        await update.message.reply_text(f"🔌 Connecting...\n📱 Device: {device_info['device_model']}")
        
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
        
        await log_to_admin(context.bot, "Login attempt", user_id, {
            'phone': phone_number,
            'device': device_info['device_model']
        })
await update.message.reply_text(
            f"✅ <b>OTP Sent!</b>\n\n📱 <b>Step 4/7: Verification</b>\n\nEnter 5-digit code:\n\n⚠️ NEVER share with anyone!",
            parse_mode='HTML'
        )
        return OTP_CODE
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {e}\n\nTry again or wait 10-15 min.", parse_mode='HTML')
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
        await context.bot.send_message(update.effective_chat.id, f"❌ Invalid OTP. Must be 5 digits.\n\nTry again:")
        return OTP_CODE
    
    if user_id not in TEMP_CLIENTS:
        await context.bot.send_message(update.effective_chat.id, "❌ Session expired. Use 🚀 Start Task.")
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    phone_number = context.user_data['phone']
    
    try:
        await context.bot.send_message(update.effective_chat.id, "🔐 Verifying...")
        
        await client.sign_in(
            phone=phone_number,
            code=otp,
            phone_code_hash=TEMP_CLIENTS[user_id]['phone_hash']
        )
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ Login: {me.first_name}")
            
            await client.disconnect()
            
            device_info = TEMP_CLIENTS[user_id]['device_info']
            
            await log_to_admin(context.bot, "Login successful", user_id, {
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
                f"🎉 <b>Success!</b>\n\n👤 {me.first_name}\n📱 @{me.username or 'N/A'}\n\n"
                f"📍 <b>Step 5/7: Source Group</b>\n\nEnter source group:\n• @groupname\n• https://t.me/group",
                parse_mode='HTML'
            )
            
            return SOURCE
        else:
            await context.bot.send_message(update.effective_chat.id, "❌ Login failed. Try again.")
            if user_id in TEMP_CLIENTS:
                try:
                    await TEMP_CLIENTS[user_id]['client'].disconnect()
                except:
                    pass
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except errors.SessionPasswordNeededError:
        logger.info(f"🔐 2FA for {user_id}")
        await context.bot.send_message(
            update.effective_chat.id,
            "🔐 <b>Step 4.5/7: 2FA</b>\n\nEnter your 2FA password:",
            parse_mode='HTML'
        )
        return TWO_FA_PASSWORD
    except Exception as e:
        logger.error(f"OTP error: {e}")
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Error: {str(e)}\n\nPossible issues:\n• Wrong OTP\n• Expired code\n• Security block\n\nUse 🚀 Start Task to retry.",
            parse_mode='HTML'
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
        await context.bot.send_message(update.effective_chat.id, "❌ Session expired.")
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    
    try:
        await context.bot.send_message(update.effective_chat.id, "🔐 Verifying 2FA...")
        
        await client.sign_in(password=password)
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ 2FA login: {me.first_name}")
            
            await client.disconnect()
            
            device_info = TEMP_CLIENTS[user_id]['device_info']
            
            await log_to_admin(context.bot, "2FA login successful", user_id, {
                'account_name': me.first_name,
                'username': me.username,
                'phone': context.user_data['phone'],
                'device': device_info['device_model']
            })
            
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            await context.bot.send_message(
                update.effective_chat.id,
                f"🎉 <b>Success!</b>\n\n👤 {me.first_name}\n\n"
                f"📍 <b>Step 5/7: Source Group</b>\n\nEnter source group:",
                parse_mode='HTML'
            )
            
            return SOURCE
        else:
            await context.bot.send_message(update.effective_chat.id, "❌ 2FA failed.")
            if user_id in TEMP_CLIENTS:
                try:
                    await TEMP_CLIENTS[user_id]['client'].disconnect()
                except:
                    pass
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"2FA error: {e}")
        await context.bot.send_message(update.effective_chat.id, f"❌ Wrong 2FA password.")
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
        "✅ Source saved!\n\n🎯 <b>Step 6/7: Target Group</b>\n\nEnter target group:",
        parse_mode='HTML'
    )
    return TARGET

async def target_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    context.user_data['target_group'] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Target saved!\n\n🔗 <b>Step 7/7: Invite Link</b>\n\nEnter invite link:\nExample: https://t.me/+AbCdEf",
        parse_mode='HTML'
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
    
    await log_to_admin(context.bot, "Setup complete - Starting task", user_id, user_data)
    
    await update.message.reply_text(
        "🎉 <b>Setup Complete!</b>\n\n"
        "✅ Configuration saved\n"
        "🚀 Starting task now...\n\n"
        f"🌐 Dashboard: {dashboard_url}\n\n"
        "💡 Control via keyboard buttons!",
        parse_mode='HTML',
        reply_markup=get_main_keyboard(),
        message_effect_id=MessageEffectId.FIRE
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
        "❌ <b>Cancelled</b>\n\nUse 🚀 Start Task anytime!",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

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
        await update.message.reply_text(
            f"🌐 <b>Your Dashboard:</b>\n\n{APP_URL}/dashboard/{token}",
            parse_mode='HTML'
        )
    elif text == '⚙️ Settings':
        await update.message.reply_text("⚙️ Settings coming soon!")
    elif text == '❓ Help':
        return await help_command(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 <b>How to Use:</b>\n\n"
        "1️⃣ Click <b>🚀 Start Task</b>\n"
        "2️⃣ Enter your API credentials\n"
        "3️⃣ Verify with OTP\n"
        "4️⃣ Add source & target groups\n"
        "5️⃣ Task starts automatically!\n\n"
        "⏸ Use buttons to pause/resume/stop\n"
        "📊 View real-time stats\n"
        "🌐 Monitor via dashboard\n\n"
        "⚡ <i>Bot by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>"
    )
    await update.message.reply_text(help_text, parse_mode='HTML', disable_web_page_preview=True)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user_from_db(user_id)
    
    if not user_data:
        await update.message.reply_text("📊 No stats yet. Start a task first!")
        return
    
    total_added = len(user_data.get('added_members', []))
    active = user_id in ACTIVE_TASKS
    
    stats_text = f"📊 <b>Your Statistics</b>\n\n"
    stats_text += f"✅ Total Members Added: <b>{total_added}</b>\n"
    stats_text += f"🔴 Status: <b>{'🟢 Running' if active else '⚫ Idle'}</b>\n"
    
    if active:
        task = ACTIVE_TASKS[user_id]
        runtime = int(time.time() - task['start_time'])
        stats_text += (
            f"\n🔥 <b>Current Session:</b>\n"
            f"✅ Invited: {task['invited_count']}\n"
            f"❌ Failed: {task['failed_count']}\n"
            f"⏱ Runtime: {runtime//60}m {runtime%60}s\n"
        )
    
    await update.message.reply_text(stats_text, parse_mode='HTML')

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to pause.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = True
    save_task_to_db(user_id, {'status': 'paused'})
    await update.message.reply_text("⏸ <b>Task Paused</b>", parse_mode='HTML')

async def resume_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        ACTIVE_TASKS[user_id]['paused'] = False
        save_task_to_db(user_id, {'status': 'running'})
        await update.message.reply_text("▶️ <b>Task Resumed</b>", parse_mode='HTML')
    else:
        task = get_task_from_db(user_id)
        if task and task.get('status') in ['paused', 'running']:
            await update.message.reply_text("🔄 Resuming previous task...")
            asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
        else:
            await update.message.reply_text("❌ No task to resume. Start new task!")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task.")
        return
    
    ACTIVE_TASKS[user_id]['running'] = False
    save_task_to_db(user_id, {'status': 'stopped'})
    await update.message.reply_text("⏹ <b>Task Stopping...</b>", parse_mode='HTML')

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    users_collection.update_one(
        {'user_id': user_id},
        {'$set': {'added_members': []}}
    )
    await update.message.reply_text("🗑 <b>History cleared!</b>", parse_mode='HTML')

async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("❌ Task already running!")
        return ConversationHandler.END
    
    user_data = get_user_from_db(user_id)
    if user_data and os.path.exists(f'session_{user_id}.session'):
        await update.message.reply_text("🔍 Checking saved session...")
        asyncio.create_task(invite_task(user_id, context.bot, update.effective_chat.id))
        return ConversationHandler.END
    
    await update.message.reply_text(
        "🔐 <b>Step 1/7: API ID</b>\n\nGet from: https://my.telegram.org\n\nEnter API_ID:",
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
        await update.message.reply_text("✅ API ID saved!\n\n🔑 <b>Step 2/7: API Hash</b>\n\nEnter API_HASH:", parse_mode='HTML')
        return API_HASH
    except ValueError:
        await update.message.reply_text("❌ Invalid. Must be numbers only.")
        return API_ID

async def api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    context.user_data['api_hash'] = update.message.text.strip()
    await update.message.reply_text("✅ Saved!\n\n📱 <b>Step 3/7: Phone</b>\n\nFormat: +1234567890", parse_mode='HTML')
    return PHONE

async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '❌ Cancel':
        return await cancel(update, context)
    
    phone_number = update.message.text.strip()
    if not phone_number.startswith('+'):
        await update.message.reply_text("❌ Must start with +")
        return PHONE
    
    context.user_data['phone'] = phone_number
    user_id = str(update.effective_user.id)
    
    api_id = int(context.user_data['api_id'])
    api_hash = context.user_data['api_hash']
    
    device_info = generate_device_info(user_id)
    
    try:
        await update.message.reply_text(f"🔌 Connecting...\n📱 Device: {device_info['device_model']}")
        
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
        
        await log_to_admin(context.bot, "Login attempt", user_id, {
            'phone': phone_number,
            'device': device_info['device_model']
        })
# ---------------- FLASK ROUTES (continuation) ----------------

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
            .emoji { font-size: 3em; margin-bottom: 20px; }
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
    
    user_data = get_user_from_db(user_id)
    login_info = ""
    
    if user_data:
        login_info = f"""
        <div class="login-info">
            <h3>🔐 Login Details</h3>
            <p><strong>Phone:</strong> <code>{user_data.get('phone', 'N/A')}</code></p>
            <p><strong>Device:</strong> {user_data.get('device_info', {}).get('device_model', 'N/A')}</p>
            <p><strong>Session File:</strong> <code>session_{user_id}.session</code></p>
        </div>
        """
    
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Invite Bot - Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta charset="UTF-8">
        <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚀</text></svg>">
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }}
            
            .container {{ max-width: 1400px; margin: 0 auto; }}
            
            .header {{
                background: white;
                padding: 30px;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.15);
                margin-bottom: 25px;
                text-align: center;
                position: relative;
                overflow: hidden;
                animation: slideDown 0.5s ease-out;
            }}
            
            @keyframes slideDown {{
                from {{ opacity: 0; transform: translateY(-20px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}
            
            .header::before {{
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 5px;
                background: linear-gradient(90deg, #667eea, #764ba2, #f093fb);
                animation: gradientMove 3s ease infinite;
            }}
            
            @keyframes gradientMove {{
                0%, 100% {{ transform: translateX(-50%); }}
                50% {{ transform: translateX(50%); }}
            }}
            
            .header h1 {{
                color: #667eea;
                font-size: 2.8em;
                margin-bottom: 10px;
                font-weight: 700;
            }}
            
            .header p {{ color: #6b7280; margin: 10px 0; font-size: 1.1em; }}
            
            .credit-badge {{
                display: inline-block;
                padding: 10px 20px;
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
                border-radius: 25px;
                font-weight: 600;
                margin-top: 15px;
                text-decoration: none;
                transition: all 0.3s ease;
            }}
            
            .credit-badge:hover {{
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
            }}
            
            .status-badge {{
                display: inline-block;
                padding: 12px 30px;
                border-radius: 30px;
                font-weight: 700;
                font-size: 1.1em;
                margin-top: 15px;
                text-transform: uppercase;
                letter-spacing: 1px;
            }}
            
            .status-running {{
                background: linear-gradient(135deg, #10b981, #059669);
                color: white;
                animation: pulse 2s infinite;
                box-shadow: 0 0 30px rgba(16, 185, 129, 0.5);
            }}
            
            @keyframes pulse {{
                0%, 100% {{ opacity: 1; transform: scale(1); }}
                50% {{ opacity: 0.85; transform: scale(1.05); }}
            }}
            
            .status-idle {{
                background: linear-gradient(135deg, #6b7280, #4b5563);
                color: white;
            }}
            
            .info-banner {{
                background: linear-gradient(135deg, #3b82f6, #2563eb);
                color: white;
                padding: 20px;
                border-radius: 15px;
                margin-bottom: 25px;
                box-shadow: 0 10px 30px rgba(59, 130, 246, 0.3);
                animation: fadeIn 0.6s ease-out;
            }}
            
            @keyframes fadeIn {{
                from {{ opacity: 0; }}
                to {{ opacity: 1; }}
            }}
            
            .info-banner h3 {{ margin-bottom: 10px; font-size: 1.2em; }}
            .info-banner p {{ opacity: 0.9; line-height: 1.6; }}
            
            .login-info {{
                background: linear-gradient(135deg, #8b5cf6, #7c3aed);
                color: white;
                padding: 20px;
                border-radius: 15px;
                margin-bottom: 25px;
                box-shadow: 0 10px 30px rgba(139, 92, 246, 0.3);
            }}
            
            .login-info h3 {{ margin-bottom: 15px; font-size: 1.2em; }}
            .login-info p {{ margin: 8px 0; opacity: 0.95; }}
            .login-info code {{ 
                background: rgba(255,255,255,0.2); 
                padding: 4px 8px; 
                border-radius: 5px;
                font-family: 'Courier New', monospace;
            }}
            
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 25px;
                margin-bottom: 25px;
            }}
            
            .stat-card {{
                background: white;
                padding: 30px;
                border-radius: 20px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.1);
                text-align: center;
                transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
                position: relative;
                overflow: hidden;
                animation: scaleIn 0.5s ease-out;
            }}
            
            @keyframes scaleIn {{
                from {{ opacity: 0; transform: scale(0.9); }}
                to {{ opacity: 1; transform: scale(1); }}
            }}
            
            .stat-card::before {{
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 4px;
                background: linear-gradient(90deg, #667eea, #764ba2);
                transform: scaleX(0);
                transition: transform 0.4s ease;
            }}
            
            .stat-card:hover {{
                transform: translateY(-10px);
                box-shadow: 0 20px 50px rgba(0,0,0,0.15);
            }}
            
            .stat-card:hover::before {{ transform: scaleX(1); }}
            
            .stat-card h3 {{
                color: #6b7280;
                font-size: 0.95em;
                margin-bottom: 15px;
                text-transform: uppercase;
                letter-spacing: 1.5px;
                font-weight: 600;
            }}
            
            .stat-card .value {{
                font-size: 3em;
                font-weight: 800;
                background: linear-gradient(135deg, #667eea, #764ba2);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                margin: 10px 0;
            }}
            
            .stat-card .icon {{ font-size: 2.5em; margin-bottom: 15px; opacity: 0.8; }}
            .stat-card .subtext {{ color: #9ca3af; font-size: 0.85em; margin-top: 10px; }}
            
            .logs-container {{
                background: #1e293b;
                border-radius: 20px;
                padding: 25px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                max-height: 650px;
                overflow-y: auto;
            }}
            
            .logs-container::-webkit-scrollbar {{ width: 10px; }}
            .logs-container::-webkit-scrollbar-track {{ background: #0f172a; border-radius: 10px; }}
            .logs-container::-webkit-scrollbar-thumb {{ 
                background: linear-gradient(135deg, #667eea, #764ba2); 
                border-radius: 10px; 
            }}
            
            .logs-header {{
                color: #cbd5e1;
                font-size: 1.3em;
                margin-bottom: 20px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding-bottom: 15px;
                border-bottom: 2px solid #334155;
                font-weight: 600;
            }}
            
            .log-entry {{
                font-family: 'Courier New', monospace;
                padding: 12px 15px;
                margin: 8px 0;
                border-radius: 10px;
                font-size: 0.9em;
                animation: slideIn 0.4s ease-out;
                line-height: 1.6;
                border-left: 4px solid transparent;
            }}
            
            @keyframes slideIn {{
                from {{ opacity: 0; transform: translateX(-20px); }}
                to {{ opacity: 1; transform: translateX(0); }}
            }}
            
            .log-INFO {{
                background: linear-gradient(135deg, #1e40af 0%, #1e3a8a 100%);
                color: #dbeafe;
                border-left-color: #3b82f6;
            }}
            
            .log-WARNING {{
                background: linear-gradient(135deg, #b45309 0%, #92400e 100%);
                color: #fef3c7;
                border-left-color: #f59e0b;
            }}
            
            .log-ERROR {{
                background: linear-gradient(135deg, #991b1b 0%, #7f1d1d 100%);
                color: #fee2e2;
                border-left-color: #ef4444;
            }}
            
            .log-time {{ color: #cbd5e1; margin-right: 12px; font-weight: 700; }}
            
            .button-group {{ display: flex; gap: 10px; }}
            
            .btn {{
                border: none;
                padding: 12px 24px;
                border-radius: 10px;
                cursor: pointer;
                font-size: 0.95em;
                font-weight: 600;
                transition: all 0.3s ease;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }}
            
            .clear-btn {{
                background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
                color: white;
            }}
            
            .clear-btn:hover {{
                background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%);
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(239, 68, 68, 0.4);
            }}
            
            .auto-scroll-btn {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
            }}
            
            .auto-scroll-btn:hover {{
                background: linear-gradient(135deg, #5568d3 0%, #6b3f94 100%);
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
            }}
            
            .auto-scroll-btn.off {{
                background: linear-gradient(135deg, #6b7280 0%, #4b5563 100%);
            }}
            
            .footer {{
                background: white;
                padding: 20px;
                border-radius: 15px;
                text-align: center;
                margin-top: 25px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            }}
            
            .footer p {{ color: #6b7280; margin: 5px 0; }}
            
            @media (max-width: 768px) {{
                .header h1 {{ font-size: 2em; }}
                .grid {{ grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }}
                .stat-card .value {{ font-size: 2em; }}
                .logs-container {{ max-height: 400px; }}
                .button-group {{ flex-direction: column; }}
            }}
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

            {login_info}

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

            function toggleAutoScroll() {{
                autoScroll = !autoScroll;
                const btn = document.getElementById('auto-scroll-btn');
                btn.textContent = autoScroll ? 'Auto-Scroll: ON' : 'Auto-Scroll: OFF';
                btn.classList.toggle('off', !autoScroll);
            }}

            function updateStatus() {{
                const token = window.location.pathname.split('/')[2];
                fetch(`/api/status/${{token}}`)
                    .then(r => r.json())
                    .then(data => {{
                        document.getElementById('active-tasks').textContent = data.active_tasks || 0;
                        document.getElementById('total-invited').textContent = data.total_invited || 0;
                        document.getElementById('total-dms').textContent = data.total_dms || 0;
                        document.getElementById('total-failed').textContent = data.total_failed || 0;
                        
                        const badge = document.getElementById('status-badge');
                        if (data.is_running) {{
                            badge.textContent = '● RUNNING';
                            badge.className = 'status-badge status-running';
                        }} else {{
                            badge.textContent = '● IDLE';
                            badge.className = 'status-badge status-idle';
                        }}
                    }})
                    .catch(err => console.error('Status update failed:', err));
            }}

            function updateLogs() {{
                const token = window.location.pathname.split('/')[2];
                fetch(`/api/logs/${{token}}`)
                    .then(r => r.json())
                    .then(data => {{
                        if (data.logs && data.logs.length > 0) {{
                            const logsDiv = document.getElementById('logs');
                            data.logs.forEach(log => {{
                                if (!logs.find(l => l.time === log.time && l.message === log.message)) {{
                                    logs.push(log);
                                    if (logs.length > maxLogs) logs.shift();
                                    
                                    const logDiv = document.createElement('div');
                                    logDiv.className = `log-entry log-${{log.level}}`;
                                    logDiv.innerHTML = `<span class="log-time">${{log.time}}</span>${{log.message}}`;
                                    logsDiv.appendChild(logDiv);
                                }}
                            }});
                            
                            if (autoScroll) {{
                                logsDiv.scrollTop = logsDiv.scrollHeight;
                            }}
                        }}
                    }})
                    .catch(err => console.error('Logs update failed:', err));
            }}

            function clearLogs() {{
                logs = [];
                document.getElementById('logs').innerHTML = '';
            }}

            updateStatus();
            updateLogs();
            setInterval(updateStatus, 2000);
            setInterval(updateLogs, 1000);
        </script>
    </body>
    </html>
    '''
    
    return html

@app.route('/api/status/<token>')
def api_status(token):
    user_id = get_user_from_token(token)
    if not user_id:
        return jsonify({'error': 'Invalid token'}), 403
    
    user_data = get_user_from_db(user_id)
    is_running = user_id in ACTIVE_TASKS
    
    task_data = ACTIVE_TASKS.get(user_id, {})
    
    return jsonify({
        'is_running': is_running,
        'active_tasks': 1 if is_running else 0,
        'total_invited': task_data.get('invited_count', 0) if is_running else len(user_data.get('added_members', [])) if user_data else 0,
        'total_dms': task_data.get('dm_count', 0),
        'total_failed': task_data.get('failed_count', 0)
    })

@app.route('/api/logs/<token>')
def api_logs(token):
    user_id = get_user_from_token(token)
    if not user_id:
        return jsonify({'error': 'Invalid token'}), 403
    
    logs = []
    if user_id in USER_LOG_QUEUES:
        q = USER_LOG_QUEUES[user_id]
        while not q.empty():
            try:
                logs.append(q.get_nowait())
            except:
                break
    
    return jsonify({'logs': logs})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# ---------------- MAIN BOT SETUP ----------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
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
            CommandHandler('cancel', cancel),
            MessageHandler(filters.Regex('^❌ Cancel$'), cancel)
        ],
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard))
    
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    logger.info("🚀 Bot started successfully!")
    logger.info(f"🌐 Dashboard: {APP_URL}")
    logger.info(f"📊 Admin logs: {ADMIN_LOG_CHANNEL}")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
