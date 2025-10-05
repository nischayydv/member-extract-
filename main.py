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
API_ID, API_HASH, PHONE, SOURCE, TARGET, INVITE_LINK = range(6)

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
        logger.error(f"Error loading {filename}: {e}")
    return default

USER_HISTORY = load_json_file(USER_DATA_FILE, {})
STATS = load_json_file(STATS_FILE, {})

# ---------------- ACTIVE TASKS ----------------
ACTIVE_TASKS = {}

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
        'min_delay': int(settings.get('min_delay', 4)),
        'max_delay': int(settings.get('max_delay', 10)),
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
    """Main invite task with enhanced features"""
    try:
        data = USER_HISTORY.get(user_id)
        if not data:
            await update.message.reply_text("❌ No configuration found. Use /run first.")
            return

        settings = get_user_settings(user_id)
        if not settings:
            settings = {
                'min_delay': 4,
                'max_delay': 10,
                'pause_time': 600,
                'max_invites': 0,
                'filter_online': False,
                'filter_verified': False,
                'skip_dm_on_fail': False,
                'custom_message': None
            }
        
        init_user_stats(user_id)
        
        session_name = f'session_{user_id}'
        sent_file = f'sent_{user_id}.txt'
        invited_file = f'invited_{user_id}.txt'
        failed_file = f'failed_{user_id}.txt'

        # Task tracking
        ACTIVE_TASKS[user_id] = {
            'running': True,
            'paused': False,
            'invited_count': 0,
            'dm_count': 0,
            'failed_count': 0,
            'start_time': time.time()
        }

        client = TelegramClient(session_name, int(data['api_id']), data['api_hash'])
        
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.start(phone=data['phone'])
            
            me = await client.get_me()
            await update.message.reply_text(f"✅ Logged in as: {me.first_name} (@{me.username or 'N/A'})")
        except Exception as e:
            await update.message.reply_text(f"❌ Login failed: {e}")
            if user_id in ACTIVE_TASKS:
                del ACTIVE_TASKS[user_id]
            await client.disconnect()
            return

        try:
            target_entity = await client.get_entity(data['target_group'])
            source_entity = await client.get_entity(data['source_group'])
            
            await update.message.reply_text("📊 Fetching members from source group...")
            participants = await client.get_participants(source_entity, limit=None)
            
            target_name = getattr(target_entity, 'title', data['target_group'])
            await update.message.reply_text(
                f"📋 **Task Started**\n\n"
                f"👥 Source Members: {len(participants)}\n"
                f"🎯 Target: {target_name}\n"
                f"⏱️ Delay: {settings['min_delay']}-{settings['max_delay']}s\n"
                f"{'📊 Max Invites: ' + str(settings['max_invites']) if settings['max_invites'] > 0 else '♾️ Unlimited invites'}\n\n"
                f"Use /pause to pause\nUse /stop to stop"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error fetching groups: {e}")
            if user_id in ACTIVE_TASKS:
                del ACTIVE_TASKS[user_id]
            await client.disconnect()
            return

        # Load processed users
        invited_set = load_set(invited_file)
        sent_set = load_set(sent_file)
        failed_set = load_set(failed_file)
        
        processed = 0
        max_invites = settings['max_invites']

        for user in participants:
            # Check if task is stopped
            if not ACTIVE_TASKS.get(user_id, {}).get('running', False):
                await update.message.reply_text("⏹️ Task stopped by user.")
                break

            # Check if task is paused
            while ACTIVE_TASKS.get(user_id, {}).get('paused', False):
                await asyncio.sleep(2)

            # Check max invites limit
            if max_invites > 0 and ACTIVE_TASKS[user_id]['invited_count'] >= max_invites:
                await update.message.reply_text(f"✅ Reached maximum invite limit ({max_invites})")
                break

            uid = str(getattr(user, 'id', ''))
            if not uid or uid in invited_set or getattr(user, 'bot', False) or getattr(user, 'is_self', False):
                continue

            # Filter by online status
            if settings['filter_online']:
                status = getattr(user, 'status', None)
                if not isinstance(status, (UserStatusOnline, UserStatusRecently)):
                    continue

            # Filter verified users
            if settings['filter_verified'] and not getattr(user, 'verified', False):
                continue

            processed += 1
            first_name = getattr(user, 'first_name', 'User') or 'User'
            username = getattr(user, 'username', None)
            display = f"{first_name} (@{username})" if username else first_name

            # Try to invite
            invited_ok, info = await try_invite(client, target_entity, user)
            
            if invited_ok:
                append_line(invited_file, uid)
                invited_set.add(uid)
                ACTIVE_TASKS[user_id]['invited_count'] += 1
                STATS[user_id]['total_invited'] += 1
                
                if processed % 5 == 0:  # Report every 5 users
                    await update.message.reply_text(f"✅ Invited: {display} | Total: {ACTIVE_TASKS[user_id]['invited_count']}")
                
                await asyncio.sleep(random.uniform(settings['min_delay'], settings['max_delay']))
                continue

            # Handle errors
            if info and info.startswith('floodwait'):
                try:
                    wait_time = int(info.split(':')[1])
                    await update.message.reply_text(
                        f"⚠️ FloodWait detected!\n"
                        f"⏳ Waiting {wait_time} seconds..."
                    )
                    await asyncio.sleep(wait_time + 5)
                    continue
                except:
                    await asyncio.sleep(settings['pause_time'])
                    continue
            
            if info == 'peerflood':
                await update.message.reply_text(
                    f"⚠️ PeerFlood detected!\n"
                    f"⏳ Pausing for {settings['pause_time']//60} minutes..."
                )
                await asyncio.sleep(settings['pause_time'])
                continue

            # Try sending DM if invite failed
            if not settings['skip_dm_on_fail'] and uid not in sent_set:
                try:
                    custom_msg = settings.get('custom_message')
                    if custom_msg:
                        text = custom_msg.replace('{name}', first_name).replace('{link}', data['invite_link'])
                    else:
                        text = f"Hi {first_name}! 👋\n\nJoin our group: {data['invite_link']}"
                    
                    await client.send_message(uid, text)
                    append_line(sent_file, uid)
                    sent_set.add(uid)
                    ACTIVE_TASKS[user_id]['dm_count'] += 1
                    STATS[user_id]['total_dms_sent'] += 1
                    
                    if processed % 5 == 0:
                        await update.message.reply_text(f"📧 DM sent: {display}")
                    
                    await asyncio.sleep(random.uniform(settings['min_delay'], settings['max_delay']))
                except Exception as e:
                    append_line(failed_file, uid)
                    failed_set.add(uid)
                    ACTIVE_TASKS[user_id]['failed_count'] += 1
                    STATS[user_id]['total_failed'] += 1
                    logger.error(f"DM failed for {uid}: {e}")
            else:
                append_line(failed_file, uid)
                failed_set.add(uid)
                ACTIVE_TASKS[user_id]['failed_count'] += 1
                STATS[user_id]['total_failed'] += 1

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
    user_id = str(update.effective_user.id)
    keyboard = [
        ['🚀 Start New Task', '🔄 Repeat Last Task'],
        ['📊 Statistics', '⚙️ Settings'],
        ['❓ Help', '📋 My Info']
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome_msg = (
        "👋 **Welcome to Advanced Invite Bot!**\n\n"
        "🎯 Features:\n"
        "• Bulk invite members\n"
        "• Smart DM fallback\n"
        "• Flood protection\n"
        "• Custom settings\n"
        "• Detailed statistics\n"
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
        "⚙️ /settings - Configure bot\n"
        "🗑️ /clear - Clear history\n"
        "📋 /info - Show your info\n"
        "❌ /cancel - Cancel operation\n\n"
        "💡 **Tips:**\n"
        "• Keep delays between 4-10 seconds\n"
        "• Use custom messages for better response\n"
        "• Monitor for flood warnings\n"
        "• Pause if needed and resume later"
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

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Info command handler"""
    user_id = str(update.effective_user.id)
    
    if user_id not in USER_HISTORY:
        await update.message.reply_text("❌ No data found. Use /run to start.")
        return
    
    data = USER_HISTORY[user_id]
    settings = get_user_settings(user_id)
    
    if not settings:
        settings = {
            'min_delay': 4,
            'max_delay': 10,
            'pause_time': 600,
            'max_invites': 0,
            'filter_online': False,
            'filter_verified': False
        }
    
    info_text = (
        f"📋 **Your Configuration**\n\n"
        f"📱 Phone: {data.get('phone', 'N/A')}\n"
        f"📥 Source: {data.get('source_group', 'N/A')}\n"
        f"📤 Target: {data.get('target_group', 'N/A')}\n"
        f"🔗 Invite Link: {data.get('invite_link', 'N/A')}\n\n"
        f"⚙️ **Settings:**\n"
        f"⏱️ Delay: {settings['min_delay']}-{settings['max_delay']}s\n"
        f"⏸️ Pause Time: {settings['pause_time']}s\n"
        f"📊 Max Invites: {settings['max_invites'] if settings['max_invites'] > 0 else 'Unlimited'}\n"
        f"🟢 Filter Online: {'Yes' if settings['filter_online'] else 'No'}\n"
        f"✅ Filter Verified: {'Yes' if settings['filter_verified'] else 'No'}"
    )
    
    await update.message.reply_text(info_text)

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause command handler"""
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to pause.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = True
    await update.message.reply_text("⏸️ Task paused. Use /resume to continue.")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume command handler"""
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to resume.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = False
    await update.message.reply_text("▶️ Task resumed.")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop command handler"""
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to stop.")
        return
    
    ACTIVE_TASKS[user_id]['running'] = False
    await update.message.reply_text("⏹️ Task stopping...")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear history command handler"""
    user_id = str(update.effective_user.id)
    
    files = [f'sent_{user_id}.txt', f'invited_{user_id}.txt', f'failed_{user_id}.txt']
    for f in files:
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception as e:
            logger.error(f"Error removing {f}: {e}")
    
    await update.message.reply_text("🗑️ History cleared!")

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Settings menu handler"""
    keyboard = [
        ['⏱️ Set Delays', '⏸️ Set Pause Time'],
        ['📊 Set Max Invites', '💬 Custom Message'],
        ['🟢 Toggle Online Filter', '✅ Toggle Verified Filter'],
        ['🔙 Back to Main']
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("⚙️ **Settings Menu**\nChoose an option:", reply_markup=reply_markup)

# ---------------- CONVERSATION HANDLERS ----------------
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run command - start conversation"""
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("⚠️ A task is already running! Use /stop first.")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "🔑 **Step 1/6: API ID**\n\n"
        "Enter your API_ID from my.telegram.org:",
        reply_markup=ReplyKeyboardRemove()
    )
    return API_ID

async def api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle API ID input"""
    try:
        api_id_value = update.message.text.strip()
        # Validate it's numeric
        int(api_id_value)
        context.user_data['api_id'] = api_id_value
        await update.message.reply_text("🔐 **Step 2/6: API Hash**\n\nEnter your API_HASH:")
        return API_HASH
    except ValueError:
        await update.message.reply_text("❌ Invalid API ID. Please enter numbers only:")
        return API_ID

async def api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle API Hash input"""
    context.user_data['api_hash'] = update.message.text.strip()
    await update.message.reply_text(
        "📱 **Step 3/6: Phone Number**\n\n"
        "Enter your phone number with country code:\n"
        "Example: +1234567890"
    )
    return PHONE

async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle phone number input"""
    phone_number = update.message.text.strip()
    if not phone_number.startswith('+'):
        await update.message.reply_text("⚠️ Phone should start with + and country code. Try again:")
        return PHONE
    
    context.user_data['phone'] = phone_number
    await update.message.reply_text(
        "📥 **Step 4/6: Source Group**\n\n"
        "Enter source group username or link:\n"
        "Example: @groupname or https://t.me/groupname"
    )
    return SOURCE

async def source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle source group input"""
    context.user_data['source_group'] = update.message.text.strip()
    await update.message.reply_text(
        "📤 **Step 5/6: Target Group**\n\n"
        "Enter target group username or link:"
    )
    return TARGET

async def target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle target group input"""
    context.user_data['target_group'] = update.message.text.strip()
    await update.message.reply_text(
        "🔗 **Step 6/6: Invite Link**\n\n"
        "Enter the invite link for your target group:"
    )
    return INVITE_LINK

async def invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle invite link input and start task"""
    context.user_data['invite_link'] = update.message.text.strip()
    user_id = str(update.effective_user.id)

    # Initialize settings if not exists
    if 'settings' not in context.user_data:
        existing_settings = get_user_settings(user_id)
        if existing_settings:
            context.user_data['settings'] = existing_settings
        else:
            context.user_data['settings'] = {
                'min_delay': 4,
                'max_delay': 10,
                'pause_time': 600,
                'max_invites': 0,
                'filter_online': False,
                'filter_verified': False,
                'skip_dm_on_fail': False,
                'custom_message': None
            }

    USER_HISTORY[user_id] = dict(context.user_data)
    save_user_history()

    keyboard = [['🚀 Start Task', '📋 Main Menu']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "✅ **Configuration Saved!**\n\n"
        "Ready to start the invite task.",
        reply_markup=reply_markup
    )
    
    # Start task automatically
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
    
    await update.message.reply_text("🔄 Rerunning your last task...")
    asyncio.create_task(invite_task(user_id, update, context))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation"""
    await update.message.reply_text(
        "❌ Operation cancelled.",
        reply_markup=ReplyKeyboardMarkup([['📋 Main Menu']], resize_keyboard=True)
    )
    return ConversationHandler.END

# Message handler for keyboard buttons
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle keyboard button presses"""
    text = update.message.text
    
    if text == '🚀 Start New Task' or text == '🚀 Start Task':
        return await run_command(update, context)
    elif text == '🔄 Repeat Last Task':
        return await rerun(update, context)
    elif text == '📊 Statistics':
        return await stats_command(update, context)
    elif text == '⚙️ Settings':
        return await settings_menu(update, context)
    elif text == '❓ Help':
        return await help_command(update, context)
    elif text == '📋 My Info' or text == '📋 Main Menu':
        return await start(update, context)
    elif text == '🔙 Back to Main':
        return await start(update, context)
    else:
        # Unknown button
        return await start(update, context)

# ---------------- ERROR HANDLER ----------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Exception while handling an update: {context.error}")
    
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ An error occurred. Please try again or contact support."
            )
        except:
            pass

# ---------------- BOT SETUP ----------------
def main():
    """Main function to run the bot"""
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not found in environment variables!")
        print("❌ ERROR: BOT_TOKEN not set!")
        print("Please set BOT_TOKEN in environment variables.")
        return

    try:
        # Create conversation handler
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('run', run_command)],
            states={
                API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_id)],
                API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_hash)],
                PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone)],
                SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, source)],
                TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, target)],
                INVITE_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, invite_link)]
            },
            fallbacks=[CommandHandler('cancel', cancel)],
        )

        # Build application
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        
        # Add handlers
        application.add_handler(CommandHandler('start', start))
        application.add_handler(CommandHandler('help', help_command))
        application.add_handler(CommandHandler('rerun', rerun))
        application.add_handler(CommandHandler('stats', stats_command))
        application.add_handler(CommandHandler('info', info_command))
        application.add_handler(CommandHandler('pause', pause_command))
        application.add_handler(CommandHandler('resume', resume_command))
        application.add_handler(CommandHandler('stop', stop_command))
        application.add_handler(CommandHandler('clear', clear_command))
        application.add_handler(CommandHandler('settings', settings_menu))
        application.add_handler(conv_handler)
        
        # Button handler (must be last)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))
        
        # Error handler
        application.add_error_handler(error_handler)

        # Start bot
        logger.info("🤖 Bot is running on Render...")
        print("🤖 Bot is running on Render...")
        print("✅ Press Ctrl+C to stop")
        
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
        
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"❌ Failed to start bot: {e}")

if __name__ == '__main__':
    main()
