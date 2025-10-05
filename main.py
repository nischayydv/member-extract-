import asyncio
import os
import json
import random
import time
import logging
from datetime import datetime
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ConversationHandler, 
    MessageHandler, filters, ContextTypes
)

# ---------------- LOGGING SETUP ----------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- TELEGRAM BOT TOKEN ----------------
BOT_TOKEN = os.environ.get('BOT_TOKEN')

# ---------------- STATES ----------------
API_ID, API_HASH, PHONE, OTP_CODE, TWO_FA_PASSWORD, SOURCE, TARGET, INVITE_LINK = range(8)

# ---------------- DATA FILES ----------------
USER_DATA_FILE = 'user_data.json'
STATS_FILE = 'stats.json'

# ---------------- LOAD DATA ----------------
def load_json_file(filename, default=None):
    """Safely load JSON file"""
    if default is None:
        default = {}
    try:
        if os.path.exists(filename):
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.info("🤖 Bot is starting...")
        logger.info(f"🌐 Web dashboard available at: http://localhost:{PORT}")
        logger.info("✅ All handlers registered")
        
        print("\n" + "="*60)
        print("🤖 TELEGRAM INVITE BOT - FULLY OPERATIONAL")
        print("="*60)
        print(f"📱 Bot Token: {'*' * 20}{BOT_TOKEN[-10:]}")
        print(f"🌐 Dashboard: http://localhost:{PORT}")
        print(f"📊 Health Check: http://localhost:{PORT}/health")
        print(f"🔴 Status: RUNNING")
        print("="*60 + "\n")
        
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except KeyboardInterrupt:
        logger.info("⏹️ Bot stopped by user")
        print("\n⏹️ Bot stopped gracefully")
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")
        print(f"❌ Failed to start bot: {e}")

if __name__ == '__main__':
    main().error(f"Error loading {filename}: {e}")
    return default

USER_HISTORY = load_json_file(USER_DATA_FILE, {})
STATS = load_json_file(STATS_FILE, {})

# ---------------- ACTIVE TASKS ----------------
ACTIVE_TASKS = {}
TEMP_CLIENTS = {}  # Store temporary clients during login

# ---------------- HELPER FUNCTIONS ----------------
def save_user_history():
    """Save user history to file"""
    try:
        with open(USER_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(USER_HISTORY, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving user history: {e}")

def save_stats():
    """Save statistics to file"""
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(STATS, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving stats: {e}")

def append_line(filename, line):
    """Append line to file"""
    try:
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(f"{line}\n")
    except Exception as e:
        logger.error(f"Error appending to {filename}: {e}")

def load_set(filename):
    """Load file lines into a set"""
    if not os.path.exists(filename):
        return set()
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return set(line.strip() for line in f if line.strip())
    except Exception as e:
        logger.error(f"Error loading {filename}: {e}")
        return set()

def get_user_settings(user_id):
    """Get user settings with defaults"""
    if user_id not in USER_HISTORY:
        return None
    settings = USER_HISTORY[user_id].get('settings', {})
    return {
        'min_delay': float(settings.get('min_delay', 4.0)),
        'max_delay': float(settings.get('max_delay', 10.0)),
        'pause_time': int(settings.get('pause_time', 600)),
        'max_invites': int(settings.get('max_invites', 0)),
        'filter_online': bool(settings.get('filter_online', False)),
        'filter_verified': bool(settings.get('filter_verified', False)),
        'skip_dm_on_fail': bool(settings.get('skip_dm_on_fail', False)),
        'custom_message': settings.get('custom_message', None)
    }

def init_user_stats(user_id):
    """Initialize stats for new user"""
    if user_id not in STATS:
        STATS[user_id] = {
            'total_invited': 0,
            'total_dms_sent': 0,
            'total_failed': 0,
            'total_runs': 0,
            'last_run': None
        }
        save_stats()

# ---------------- INVITE LOGIC ----------------
async def try_invite(client, target_entity, user):
    """Attempt to invite a user"""
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
    except errors.ChatWriteForbiddenError:
        return False, 'write_forbidden'
    except errors.ChannelPrivateError:
        return False, 'channel_private'
    except Exception as e:
        logger.error(f"Invite error: {type(e).__name__} - {e}")
        return False, f'error:{type(e).__name__}'

async def invite_task(user_id, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    """Main invite task"""
    try:
        data = USER_HISTORY.get(user_id)
        if not data:
            await update.message.reply_text("❌ No configuration found. Use /run first.")
            return

        settings = get_user_settings(user_id)
        if not settings:
            settings = {
                'min_delay': 4.0,
                'max_delay': 10.0,
                'pause_time': 600,
                'max_invites': 0,
                'filter_online': False,
                'filter_verified': False,
                'skip_dm_on_fail': False,
                'custom_message': None
            }
        
        init_user_stats(user_id)
        
        # File names
        session_name = f'session_{user_id}'
        sent_file = f'sent_{user_id}.txt'
        invited_file = f'invited_{user_id}.txt'

        # Task tracking
        ACTIVE_TASKS[user_id] = {
            'running': True,
            'paused': False,
            'invited_count': 0,
            'dm_count': 0,
            'failed_count': 0,
            'start_time': time.time()
        }

        # Extract variables
        api_id = int(data['api_id'])
        api_hash = data['api_hash']
        source_group = data['source_group']
        target_group = data['target_group']
        invite_link = data['invite_link']
        min_delay = settings['min_delay']
        max_delay = settings['max_delay']
        pause_time = settings['pause_time']

        # Create client
        client = TelegramClient(session_name, api_id, api_hash)
        
        try:
            await client.connect()
            
            if not await client.is_user_authorized():
                await update.message.reply_text("❌ Session expired. Please use /run to login again.")
                if user_id in ACTIVE_TASKS:
                    del ACTIVE_TASKS[user_id]
                await client.disconnect()
                return
            
            me = await client.get_me()
            await update.message.reply_text(f"✅ Logged in as: {me.first_name} (@{me.username or 'N/A'})")
        except Exception as e:
            await update.message.reply_text(f"❌ Login failed: {e}")
            if user_id in ACTIVE_TASKS:
                del ACTIVE_TASKS[user_id]
            await client.disconnect()
            return

        # Main loop
        while ACTIVE_TASKS.get(user_id, {}).get('running', False):
            try:
                # Load already processed users
                already_sent = load_set(sent_file)
                already_invited = load_set(invited_file)

                # Get entities
                target_entity = await client.get_entity(target_group)
                participants = await client.get_participants(source_group)
                
                await update.message.reply_text(
                    f"📋 **Task Started**\n\n"
                    f"👥 Total members in source group: {len(participants)}\n"
                    f"⏱️ Delay: {min_delay}-{max_delay}s\n\n"
                    f"Use /pause to pause\nUse /stop to stop"
                )

                # Process each user
                for user in participants:
                    # Check if task is stopped or paused
                    if not ACTIVE_TASKS.get(user_id, {}).get('running', False):
                        await update.message.reply_text("⏹️ Task stopped by user.")
                        break

                    while ACTIVE_TASKS.get(user_id, {}).get('paused', False):
                        await asyncio.sleep(2)

                    # Get user ID
                    uid = str(getattr(user, 'id', ''))
                    if not uid or uid in already_invited or uid in already_sent:
                        continue
                    
                    if getattr(user, 'bot', False) or getattr(user, 'is_self', False):
                        continue

                    # Display processing
                    first_name = getattr(user, 'first_name', 'User') or 'User'
                    username = getattr(user, 'username', '')
                    logger.info(f"Processing: {uid} | {first_name} | @{username}")

                    # Try to invite
                    invited_ok, info = await try_invite(client, target_entity, user)
                    
                    if invited_ok:
                        logger.info(f"[INVITED] {uid} ({info if info else 'success'})")
                        append_line(invited_file, uid)
                        already_invited.add(uid)
                        ACTIVE_TASKS[user_id]['invited_count'] += 1
                        STATS[user_id]['total_invited'] += 1
                        
                        await asyncio.sleep(random.uniform(min_delay, max_delay))
                        continue

                    # Handle FloodWait
                    if info and info.startswith('floodwait'):
                        try:
                            wait_time = int(info.split(':')[1])
                            logger.warning(f"FloodWait detected. Pausing {wait_time} seconds...")
                            await update.message.reply_text(
                                f"⚠️ FloodWait detected!\n⏳ Waiting {wait_time} seconds..."
                            )
                            await asyncio.sleep(wait_time + 5)
                            continue
                        except:
                            await asyncio.sleep(pause_time)
                            continue
                    
                    # Handle PeerFlood
                    if info == 'peerflood':
                        logger.warning(f"PeerFlood detected. Pausing {pause_time//60} minutes...")
                        await update.message.reply_text(
                            f"⚠️ PeerFlood detected!\n⏳ Pausing for {pause_time//60} minutes..."
                        )
                        await asyncio.sleep(pause_time)
                        continue

                    # Try sending DM
                    if not settings['skip_dm_on_fail'] and uid not in already_sent:
                        try:
                            # Custom message or default
                            custom_msg = settings.get('custom_message')
                            if custom_msg:
                                text = custom_msg.replace('{name}', first_name).replace('{link}', invite_link)
                            else:
                                text = f"Hi {first_name}! Join our group here: {invite_link}"
                            
                            await client.send_message(user.id, text)
                            logger.info(f"[MESSAGED] {uid}")
                            append_line(sent_file, uid)
                            already_sent.add(uid)
                            ACTIVE_TASKS[user_id]['dm_count'] += 1
                            STATS[user_id]['total_dms_sent'] += 1
                            
                            await asyncio.sleep(random.uniform(min_delay, max_delay))
                        except errors.UserPrivacyRestrictedError:
                            logger.info(f"Can't DM {uid}: privacy settings.")
                            ACTIVE_TASKS[user_id]['failed_count'] += 1
                        except errors.FloodWaitError as e:
                            logger.warning(f"FloodWait during DM. Pausing {pause_time//60} minutes...")
                            await asyncio.sleep(pause_time)
                        except errors.PeerFloodError:
                            logger.warning("PeerFlood detected on send. Pausing...")
                            await asyncio.sleep(pause_time)
                        except Exception as e:
                            logger.error(f"Failed to DM {uid}: {type(e).__name__} {e}")
                            ACTIVE_TASKS[user_id]['failed_count'] += 1
                    else:
                        ACTIVE_TASKS[user_id]['failed_count'] += 1

                # Batch finished
                logger.info("Batch finished. Restarting loop...")
                await update.message.reply_text("✅ Batch completed. Restarting in 30 seconds...")
                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await update.message.reply_text(f"❌ Error: {e}\nRestarting in 60 seconds...")
                await asyncio.sleep(60)

        # Task completion
        elapsed = time.time() - ACTIVE_TASKS[user_id]['start_time']
        STATS[user_id]['total_runs'] += 1
        STATS[user_id]['last_run'] = datetime.now().isoformat()
        save_stats()

        await update.message.reply_text(
            f"🎉 **Task Completed!**\n\n"
            f"✅ Invited: {ACTIVE_TASKS[user_id]['invited_count']}\n"
            f"📧 DMs Sent: {ACTIVE_TASKS[user_id]['dm_count']}\n"
            f"❌ Failed: {ACTIVE_TASKS[user_id]['failed_count']}\n"
            f"⏱️ Time: {int(elapsed//60)}m {int(elapsed%60)}s\n\n"
            f"Use /stats to see overall statistics"
        )

        if user_id in ACTIVE_TASKS:
            del ACTIVE_TASKS[user_id]
        await client.disconnect()

    except Exception as e:
        logger.error(f"Task error for user {user_id}: {e}")
        if user_id in ACTIVE_TASKS:
            del ACTIVE_TASKS[user_id]
        try:
            await update.message.reply_text(f"❌ Task error: {e}")
        except:
            pass

# ---------------- BOT COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    keyboard = [
        ['🚀 Start New Task', '🔄 Repeat Last Task'],
        ['📊 Statistics', '❓ Help']
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome_msg = (
        "👋 **Welcome to Telegram Invite Bot!**\n\n"
        "🎯 Features:\n"
        "• Bulk invite members\n"
        "• Smart DM fallback\n"
        "• Flood protection\n"
        "• Auto-restart loop\n"
        "• Pause/Resume support\n"
        "• Live web dashboard\n\n"
        "Choose an option below:":\n"
        "• Bulk invite members\n"
        "• Smart DM fallback\n"
        "• Flood protection\n"
        "• Auto-restart loop\n"
        "• Pause/Resume support\n\n"
        "Choose an option below:"
    )
    
    await update.message.reply_text(welcome_msg, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    help_text = (
        "📚 **Command Guide**\n\n"
        "🚀 /run - Start new invite task\n"
        "🔄 /rerun - Repeat last task\n"
        "⏸️ /pause - Pause running task\n"
        "▶️ /resume - Resume paused task\n"
        "⏹️ /stop - Stop running task\n"
        "📊 /stats - View statistics\n"
        "🗑️ /clear - Clear history\n"
        "❌ /cancel - Cancel operation\n\n"
        "💡 **Tips:**\n"
        "• Keep delays between 4-10 seconds\n"
        "• Monitor for flood warnings\n"
        "• Use pause if needed\n"
        "• Check live dashboard on web interface"
    )
    await update.message.reply_text(help_text)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Statistics command handler"""
    user_id = str(update.effective_user.id)
    init_user_stats(user_id)
    
    stats = STATS[user_id]
    active = user_id in ACTIVE_TASKS
    
    stats_text = (
        f"📊 **Your Statistics**\n\n"
        f"✅ Total Invited: {stats['total_invited']}\n"
        f"📧 Total DMs: {stats['total_dms_sent']}\n"
        f"❌ Total Failed: {stats['total_failed']}\n"
        f"🔄 Total Runs: {stats['total_runs']}\n"
        f"📅 Last Run: {stats['last_run'][:10] if stats['last_run'] else 'Never'}\n"
        f"🔴 Status: {'Running' if active else 'Idle'}\n\n"
    )
    
    if active:
        task = ACTIVE_TASKS[user_id]
        stats_text += (
            f"**Current Task:**\n"
            f"✅ Invited: {task['invited_count']}\n"
            f"📧 DMs: {task['dm_count']}\n"
            f"❌ Failed: {task['failed_count']}\n"
            f"⏱️ Running: {int((time.time() - task['start_time'])//60)} min"
        )
    
    await update.message.reply_text(stats_text)

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause command handler"""
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to pause.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = True
    logger.info(f"⏸️ Task paused by user {user_id}")
    await update.message.reply_text("⏸️ Task paused. Use /resume to continue.")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume command handler"""
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to resume.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = False
    logger.info(f"▶️ Task resumed by user {user_id}")
    await update.message.reply_text("▶️ Task resumed.")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop command handler"""
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to stop.")
        return
    
    ACTIVE_TASKS[user_id]['running'] = False
    logger.info(f"⏹️ Task stopped by user {user_id}")
    await update.message.reply_text("⏹️ Task stopping...")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear history command handler"""
    user_id = str(update.effective_user.id)
    
    files = [f'sent_{user_id}.txt', f'invited_{user_id}.txt']
    for f in files:
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception as e:
            logger.error(f"Error removing {f}: {e}")
    
    logger.info(f"🗑️ History cleared for user {user_id}")
    await update.message.reply_text("🗑️ History cleared!")

# ---------------- CONVERSATION HANDLERS ----------------
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run command - start conversation"""
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("⚠️ A task is already running! Use /stop first.")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "🔑 **Step 1/7: API ID**\n\n"
        "Enter your API_ID from my.telegram.org:",
        reply_markup=ReplyKeyboardRemove()
    )
    return API_ID

async def api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle API ID input"""
    try:
        api_id_value = update.message.text.strip()
        int(api_id_value)
        context.user_data['api_id'] = api_id_value
        await update.message.reply_text("🔐 **Step 2/7: API Hash**\n\nEnter your API_HASH:")
        return API_HASH
    except ValueError:
        await update.message.reply_text("❌ Invalid API ID. Please enter numbers only:")
        return API_ID

async def api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle API Hash input"""
    context.user_data['api_hash'] = update.message.text.strip()
    await update.message.reply_text(
        "📱 **Step 3/7: Phone Number**\n\n"
        "Enter your phone number with country code:\n"
        "Example: +1234567890"
    )
    return PHONE

async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle phone number input and send OTP"""
    phone_number = update.message.text.strip()
    if not phone_number.startswith('+'):
        await update.message.reply_text("⚠️ Phone should start with + and country code. Try again:")
        return PHONE
    
    context.user_data['phone'] = phone_number
    user_id = str(update.effective_user.id)
    
    # Create temporary client for login
    api_id = int(context.user_data['api_id'])
    api_hash = context.user_data['api_hash']
    session_name = f'session_{user_id}'
    
    try:
        client = TelegramClient(session_name, api_id, api_hash)
        await client.connect()
        
        # Send OTP
        await client.send_code_request(phone_number)
        TEMP_CLIENTS[user_id] = client
        
        logger.info(f"📱 OTP sent to {phone_number} for user {user_id}")
        await update.message.reply_text(
            "✅ **Step 4/7: OTP Code**\n\n"
            "An OTP has been sent to your Telegram account.\n"
            "Please enter the OTP code:"
        )
        return OTP_CODE
    except Exception as e:
        logger.error(f"Error sending OTP: {e}")
        await update.message.reply_text(f"❌ Error sending OTP: {e}\n\nPlease try again with /run")
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def otp_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle OTP code input"""
    user_id = str(update.effective_user.id)
    otp = update.message.text.strip()
    
    if user_id not in TEMP_CLIENTS:
        await update.message.reply_text("❌ Session expired. Please start again with /run")
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]
    phone_number = context.user_data['phone']
    
    try:
        await client.sign_in(phone_number, otp)
        
        # Check if logged in successfully
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ User {user_id} logged in successfully as {me.first_name}")
            await update.message.reply_text(
                f"✅ Login successful!\n\n"
                f"Logged in as: {me.first_name} (@{me.username or 'N/A'})\n\n"
                f"📥 **Step 5/7: Source Group**\n\n"
                f"Enter source group username or link:\n"
                f"Example: @groupname or https://t.me/groupname"
            )
            
            # Disconnect temp client
            await client.disconnect()
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            return SOURCE
        else:
            await update.message.reply_text("❌ Login failed. Please try again with /run")
            await client.disconnect()
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except errors.SessionPasswordNeededError:
        # Two-factor authentication required
        await update.message.reply_text(
            "🔐 **Step 4.5/7: 2FA Password**\n\n"
            "Your account has 2FA enabled.\n"
            "Please enter your 2FA password:"
        )
        return TWO_FA_PASSWORD
    except errors.PhoneCodeInvalidError:
        await update.message.reply_text("❌ Invalid OTP code. Please try again:")
        return OTP_CODE
    except Exception as e:
        logger.error(f"Error during sign in: {e}")
        await update.message.reply_text(f"❌ Error: {e}\n\nPlease try again with /run")
        await client.disconnect()
        if user_id in TEMP_CLIENTS:
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def two_fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 2FA password input"""
    user_id = str(update.effective_user.id)
    password = update.message.text.strip()
    
    if user_id not in TEMP_CLIENTS:
        await update.message.reply_text("❌ Session expired. Please start again with /run")
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]
    
    try:
        await client.sign_in(password=password)
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ User {user_id} logged in successfully with 2FA")
            await update.message.reply_text(
                f"✅ Login successful!\n\n"
                f"Logged in as: {me.first_name} (@{me.username or 'N/A'})\n\n"
                f"📥 **Step 5/7: Source Group**\n\n"
                f"Enter source group username or link:\n"
                f"Example: @groupname or https://t.me/groupname"
            )
            
            await client.disconnect()
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            return SOURCE
        else:
            await update.message.reply_text("❌ Login failed. Please try again with /run")
            await client.disconnect()
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except errors.PasswordHashInvalidError:
        await update.message.reply_text("❌ Invalid 2FA password. Please try again:")
        return TWO_FA_PASSWORD
    except Exception as e:
        logger.error(f"Error with 2FA: {e}")
        await update.message.reply_text(f"❌ Error: {e}\n\nPlease try again with /run")
        await client.disconnect()
        if user_id in TEMP_CLIENTS:
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle source group input"""
    context.user_data['source_group'] = update.message.text.strip()
    await update.message.reply_text(
        "📤 **Step 6/7: Target Group**\n\n"
        "Enter target group username or link:"
    )
    return TARGET

async def target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle target group input"""
    context.user_data['target_group'] = update.message.text.strip()
    await update.message.reply_text(
        "🔗 **Step 7/7: Invite Link**\n\n"
        "Enter the invite link for your target group:"
    )
    return INVITE_LINK

async def invite_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle invite link input and start task"""
    context.user_data['invite_link'] = update.message.text.strip()
    user_id = str(update.effective_user.id)

    # Initialize settings
    if 'settings' not in context.user_data:
        context.user_data['settings'] = {
            'min_delay': 4.0,
            'max_delay': 10.0,
            'pause_time': 600,
            'max_invites': 0,
            'filter_online': False,
            'filter_verified': False,
            'skip_dm_on_fail': False,
            'custom_message': None
        }

    USER_HISTORY[user_id] = dict(context.user_data)
    save_user_history()

    keyboard = [['📋 Main Menu']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    logger.info(f"✅ Configuration saved for user {user_id}")
    await update.message.reply_text(
        "✅ **Configuration Saved!**\n\n"
        "Starting invite task...",
        reply_markup=reply_markup
    )
    
    # Start task
    asyncio.create_task(invite_task(user_id, update, context))
    return ConversationHandler.END

async def rerun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rerun last task"""
    user_id = str(update.effective_user.id)
    
    if user_id not in USER_HISTORY:
        await update.message.reply_text("❌ No previous configuration found. Use /run first.")
        return
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("⚠️ A task is already running!")
        return
    
    logger.info(f"🔄 Rerunning task for user {user_id}")
    await update.message.reply_text("🔄 Rerunning your last task...")
    asyncio.create_task(invite_task(user_id, update, context))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation"""
    user_id = str(update.effective_user.id)
    
    # Clean up temp client if exists
    if user_id in TEMP_CLIENTS:
        try:
            await TEMP_CLIENTS[user_id].disconnect()
        except:
            pass
        del TEMP_CLIENTS[user_id]
    
    await update.message.reply_text(
        "❌ Operation cancelled.",
        reply_markup=ReplyKeyboardMarkup([['📋 Main Menu']], resize_keyboard=True)
    )
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle keyboard button presses"""
    text = update.message.text
    
    if text == '🚀 Start New Task':
        return await run_command(update, context)
    elif text == '🔄 Repeat Last Task':
        return await rerun(update, context)
    elif text == '📊 Statistics':
        return await stats_command(update, context)
    elif text == '❓ Help':
        return await help_command(update, context)
    elif text == '📋 Main Menu':
        return await start(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Exception while handling an update: {context.error}")

# ---------------- FLASK SERVER THREAD ----------------
def run_flask():
    """Run Flask server in separate thread"""
    app.run(host='0.0.0.0', port=PORT, threaded=True)

# ---------------- BOT SETUP ----------------
def main():
    """Main function to run the bot"""
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not found in environment variables!")
        print("❌ ERROR: BOT_TOKEN not set!")
        print("Please set BOT_TOKEN in environment variables.")
        return

    try:
        # Start Flask server in background thread
        flask_thread = Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info(f"🌐 Web dashboard started on port {PORT}")
        
        # Setup conversation handler with OTP support
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('run', run_command)],
            states={
                API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_id)],
                API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_hash)],
                PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone)],
                OTP_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_code)],
                TWO_FA_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, two_fa_password)],
                SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, source)],
                TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, target)],
                INVITE_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, invite_link_handler)]
            },
            fallbacks=[CommandHandler('cancel', cancel)],
        )

        application = ApplicationBuilder().token(BOT_TOKEN).build()
        
        application.add_handler(CommandHandler('start', start))
        application.add_handler(CommandHandler('help', help_command))
        application.add_handler(CommandHandler('rerun', rerun))
        application.add_handler(CommandHandler('stats', stats_command))
        application.add_handler(CommandHandler('pause', pause_command))
        application.add_handler(CommandHandler('resume', resume_command))
        application.add_handler(CommandHandler('stop', stop_command))
        application.add_handler(CommandHandler('clear', clear_command))
        application.add_handler(conv_handler)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))
        application.add_error_handler(error_handler)

        logger.info("🤖 Bot is starting...")
        logger.info(f"🌐 Access web dashboard at: http://localhost:{PORT}")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"❌ Failed to start bot: {e}")

if __name__ == '__main__':
    main()

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    help_text = (
        "📚 **Command Guide**\n\n"
        "🚀 /run - Start new invite task\n"
        "🔄 /rerun - Repeat last task\n"
        "⏸️ /pause - Pause running task\n"
        "▶️ /resume - Resume paused task\n"
        "⏹️ /stop - Stop running task\n"
        "📊 /stats - View statistics\n"
        "🗑️ /clear - Clear history\n"
        "❌ /cancel - Cancel operation\n\n"
        "💡 **Tips:**\n"
        "• Keep delays between 4-10 seconds\n
