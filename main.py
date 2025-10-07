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
from telegram.ext import (
    Application, CommandHandler, ConversationHandler, 
    MessageHandler, filters, ContextTypes
)
from flask import Flask, render_template_string, jsonify, Response
import queue

# ---------------- FLASK APP SETUP ----------------
app = Flask(__name__)
LOG_QUEUE = Queue(maxsize=1000)

# ---------------- CUSTOM LOG HANDLER ----------------
class QueueHandler(logging.Handler):
    """Custom handler to push logs to queue"""
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

# ---------------- LOGGING SETUP ----------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Add queue handler
queue_handler = QueueHandler()
queue_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
logging.getLogger().addHandler(queue_handler)

# ---------------- TELEGRAM BOT TOKEN ----------------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
PORT = int(os.environ.get('PORT', 10000))

# ---------------- STATES ----------------
API_ID, API_HASH, PHONE, OTP_CODE, TWO_FA_PASSWORD, SOURCE, TARGET, INVITE_LINK = range(8)

# ---------------- DATA FILES ----------------
USER_DATA_FILE = 'user_data.json'
STATS_FILE = 'stats.json'
DEVICE_INFO_FILE = 'device_info.json'

# ---------------- DEVICE SPOOFING ----------------
def generate_device_info(user_id):
    """Generate unique device info for each user to avoid detection"""
    device_models = [
        'Samsung SM-G991B', 'iPhone 13 Pro', 'Pixel 6', 'OnePlus 9 Pro',
        'Xiaomi Mi 11', 'Huawei P40', 'OPPO Find X3', 'Vivo X60',
        'Realme GT', 'Motorola Edge', 'Nokia 8.3', 'Sony Xperia 5'
    ]
    
    android_versions = ['11', '12', '13', '14']
    ios_versions = ['15.0', '15.1', '16.0', '16.1', '17.0']
    app_versions = ['9.4.2', '9.5.1', '9.6.0', '9.7.3', '9.8.1', '10.0.0']
    
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

def save_device_info(user_id, device_info):
    """Save device info for user"""
    try:
        devices = {}
        if os.path.exists(DEVICE_INFO_FILE):
            with open(DEVICE_INFO_FILE, 'r') as f:
                devices = json.load(f)
        
        devices[user_id] = device_info
        
        with open(DEVICE_INFO_FILE, 'w') as f:
            json.dump(devices, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving device info: {e}")

def load_device_info(user_id):
    """Load device info for user"""
    try:
        if os.path.exists(DEVICE_INFO_FILE):
            with open(DEVICE_INFO_FILE, 'r') as f:
                devices = json.load(f)
                return devices.get(user_id)
    except Exception as e:
        logger.error(f"Error loading device info: {e}")
    return None

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
TEMP_CLIENTS = {}

# ---------------- HTML TEMPLATE ----------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Telegram Invite Bot - Live Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta charset="UTF-8">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        .header {
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            margin-bottom: 20px;
            text-align: center;
        }
        .header h1 {
            color: #667eea;
            font-size: 2.5em;
            margin-bottom: 10px;
        }
        .header p {
            color: #6b7280;
            margin: 10px 0;
        }
        .status-badge {
            display: inline-block;
            padding: 10px 25px;
            border-radius: 25px;
            font-weight: bold;
            font-size: 1em;
            margin-top: 10px;
        }
        .status-running {
            background: #10b981;
            color: white;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        .status-idle {
            background: #6b7280;
            color: white;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: white;
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            text-align: center;
            transition: transform 0.3s;
        }
        .stat-card:hover {
            transform: translateY(-5px);
        }
        .stat-card h3 {
            color: #6b7280;
            font-size: 0.9em;
            margin-bottom: 10px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .stat-card .value {
            font-size: 2.5em;
            font-weight: bold;
            color: #667eea;
        }
        .stat-card .icon {
            font-size: 2em;
            margin-bottom: 10px;
        }
        .logs-container {
            background: #1e293b;
            border-radius: 15px;
            padding: 20px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            max-height: 600px;
            overflow-y: auto;
        }
        .logs-header {
            color: #94a3b8;
            font-size: 1.2em;
            margin-bottom: 15px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 10px;
            border-bottom: 2px solid #334155;
        }
        .log-entry {
            font-family: 'Courier New', monospace;
            padding: 10px 12px;
            margin: 5px 0;
            border-radius: 8px;
            font-size: 0.85em;
            animation: fadeIn 0.3s;
            line-height: 1.5;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateX(-10px); }
            to { opacity: 1; transform: translateX(0); }
        }
        .log-INFO {
            background: linear-gradient(135deg, #1e40af 0%, #1e3a8a 100%);
            color: #dbeafe;
            border-left: 4px solid #3b82f6;
        }
        .log-WARNING {
            background: linear-gradient(135deg, #b45309 0%, #92400e 100%);
            color: #fef3c7;
            border-left: 4px solid #f59e0b;
        }
        .log-ERROR {
            background: linear-gradient(135deg, #991b1b 0%, #7f1d1d 100%);
            color: #fee2e2;
            border-left: 4px solid #ef4444;
        }
        .log-time {
            color: #94a3b8;
            margin-right: 10px;
            font-weight: bold;
        }
        .clear-btn {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.9em;
            font-weight: 600;
            transition: all 0.3s;
        }
        .clear-btn:hover {
            background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%);
            transform: scale(1.05);
        }
        .auto-scroll-btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.9em;
            font-weight: 600;
            margin-right: 10px;
            transition: all 0.3s;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚀 Telegram Invite Bot</h1>
            <p>Real-Time Status Dashboard</p>
            <span id="status-badge" class="status-badge status-idle">IDLE</span>
        </div>

        <div class="grid">
            <div class="stat-card">
                <div class="icon">⚡</div>
                <h3>Active Tasks</h3>
                <div class="value" id="active-tasks">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">✅</div>
                <h3>Total Invited</h3>
                <div class="value" id="total-invited">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">💬</div>
                <h3>DMs Sent</h3>
                <div class="value" id="total-dms">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">❌</div>
                <h3>Failed</h3>
                <div class="value" id="total-failed">0</div>
            </div>
        </div>

        <div class="logs-container">
            <div class="logs-header">
                <span>📋 Live Logs</span>
                <div>
                    <button class="auto-scroll-btn" id="auto-scroll-btn" onclick="toggleAutoScroll()">
                        Auto-Scroll: ON
                    </button>
                    <button class="clear-btn" onclick="clearLogs()">Clear</button>
                </div>
            </div>
            <div id="logs"></div>
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
        }

        function updateStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('active-tasks').textContent = data.active_tasks;
                    document.getElementById('total-invited').textContent = data.total_invited;
                    document.getElementById('total-dms').textContent = data.total_dms;
                    document.getElementById('total-failed').textContent = data.total_failed;

                    const badge = document.getElementById('status-badge');
                    if (data.active_tasks > 0) {
                        badge.className = 'status-badge status-running';
                        badge.textContent = 'RUNNING (' + data.active_tasks + ')';
                    } else {
                        badge.className = 'status-badge status-idle';
                        badge.textContent = 'IDLE';
                    }
                })
                .catch(err => console.error('Status update error:', err));
        }

        function streamLogs() {
            const eventSource = new EventSource('/api/logs/stream');
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
"""

# ---------------- FLASK ROUTES ----------------
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/status')
def api_status():
    total_invited = 0
    total_dms = 0
    total_failed = 0

    for user_id, task in ACTIVE_TASKS.items():
        total_invited += task.get('invited_count', 0)
        total_dms += task.get('dm_count', 0)
        total_failed += task.get('failed_count', 0)

    return jsonify({
        'active_tasks': len(ACTIVE_TASKS),
        'total_invited': total_invited,
        'total_dms': total_dms,
        'total_failed': total_failed
    })

@app.route('/api/logs/stream')
def logs_stream():
    def generate():
        while True:
            try:
                log = LOG_QUEUE.get(timeout=30)
                yield f"data: {json.dumps(log)}

"
            except queue.Empty:
                yield f"data: {json.dumps({'time': datetime.now().strftime('%H:%M:%S'), 'level': 'INFO', 'message': '⏳ Waiting...'})}

"
            except Exception as e:
                logger.error(f"Stream error: {e}")
                break
    
    return Response(generate(), mimetype='text/event-stream')
    
@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'active_tasks': len(ACTIVE_TASKS),
        'timestamp': datetime.now().isoformat()
    })

# ---------------- HELPER FUNCTIONS ----------------
def save_user_history():
    try:
        with open(USER_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(USER_HISTORY, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving user history: {e}")

def save_stats():
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(STATS, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving stats: {e}")

def append_line(filename, line):
    try:
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(f"{line}
")
    except Exception as e:
        logger.error(f"Error appending to {filename}: {e}")

def load_set(filename):
    if not os.path.exists(filename):
        return set()
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return set(line.strip() for line in f if line.strip())
    except Exception as e:
        logger.error(f"Error loading {filename}: {e}")
        return set()

def get_user_settings(user_id):
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
        
        session_name = f'session_{user_id}'
        sent_file = f'sent_{user_id}.txt'
        invited_file = f'invited_{user_id}.txt'

        ACTIVE_TASKS[user_id] = {
            'running': True,
            'paused': False,
            'invited_count': 0,
            'dm_count': 0,
            'failed_count': 0,
            'start_time': time.time()
        }

        api_id = int(data['api_id'])
        api_hash = data['api_hash']
        source_group = data['source_group']
        target_group = data['target_group']
        invite_link = data['invite_link']
        min_delay = settings['min_delay']
        max_delay = settings['max_delay']
        pause_time = settings['pause_time']

        device_info = load_device_info(user_id)
        if device_info:
            logger.info(f"Using saved device: {device_info['device_model']}")
        else:
            logger.info("Device info not found, will use existing session")

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
            logger.info(f"User {user_id} logged in as: {me.first_name}")
            await update.message.reply_text(f"✅ Logged in as: {me.first_name} (@{me.username or 'N/A'})")
        except Exception as e:
            logger.error(f"Login failed: {e}")
            await update.message.reply_text(f"❌ Login failed: {e}")
            if user_id in ACTIVE_TASKS:
                del ACTIVE_TASKS[user_id]
            await client.disconnect()
            return

        while ACTIVE_TASKS.get(user_id, {}).get('running', False):
            try:
                already_sent = load_set(sent_file)
                already_invited = load_set(invited_file)

                target_entity = await client.get_entity(target_group)
                participants = await client.get_participants(source_group)
                
                logger.info(f"Task started | {len(participants)} members")
                await update.message.reply_text(
                    f"🚀 **Task Started**

"
                    f"👥 Members: {len(participants)}
"
                    f"⏱ Delay: {min_delay}-{max_delay}s

"
                    f"Use /pause or /stop"
                )

                for user in participants:
                    if not ACTIVE_TASKS.get(user_id, {}).get('running', False):
                        break

                    while ACTIVE_TASKS.get(user_id, {}).get('paused', False):
                        await asyncio.sleep(2)

                    uid = str(getattr(user, 'id', ''))
                    if not uid or uid in already_invited or uid in already_sent:
                        continue
                    
                    if getattr(user, 'bot', False) or getattr(user, 'is_self', False):
                        continue

                    first_name = getattr(user, 'first_name', 'User') or 'User'
                    username = getattr(user, 'username', '')

                    invited_ok, info = await try_invite(client, target_entity, user)
                    
                    if invited_ok:
                        logger.info(f"[INVITED] {uid} - {first_name}")
                        append_line(invited_file, uid)
                        already_invited.add(uid)
                        ACTIVE_TASKS[user_id]['invited_count'] += 1
                        STATS[user_id]['total_invited'] += 1
                        save_stats()
                        
                        await asyncio.sleep(random.uniform(min_delay, max_delay))
                        continue

                    if info and info.startswith('floodwait'):
                        try:
                            wait_time = int(info.split(':')[1])
                            logger.warning(f"FloodWait: {wait_time}s")
                            await update.message.reply_text(f"⚠️ FloodWait! Waiting {wait_time}s...")
                            await asyncio.sleep(wait_time + 5)
                            continue
                        except:
                            await asyncio.sleep(pause_time)
                            continue
                    
                    if info == 'peerflood':
                        logger.warning(f"PeerFlood detected")
                        await update.message.reply_text(f"⚠️ PeerFlood! Pausing {pause_time//60}min...")
                        await asyncio.sleep(pause_time)
                        continue

                    if not settings['skip_dm_on_fail'] and uid not in already_sent:
                        try:
                            custom_msg = settings.get('custom_message')
                            if custom_msg:
                                text = custom_msg.replace('{name}', first_name).replace('{link}', invite_link)
                            else:
                                text = f"Hi {first_name}! Join: {invite_link}"
                            
                            await client.send_message(user.id, text)
                            logger.info(f"[DM SENT] {uid}")
                            append_line(sent_file, uid)
                            already_sent.add(uid)
                            ACTIVE_TASKS[user_id]['dm_count'] += 1
                            STATS[user_id]['total_dms_sent'] += 1
                            save_stats()
                            
                            await asyncio.sleep(random.uniform(min_delay, max_delay))
                        except Exception as e:
                            logger.error(f"DM failed: {e}")
                            ACTIVE_TASKS[user_id]['failed_count'] += 1
                            STATS[user_id]['total_failed'] += 1
                    else:
                        ACTIVE_TASKS[user_id]['failed_count'] += 1
                        STATS[user_id]['total_failed'] += 1

                logger.info("Batch finished. Restarting in 30s...")
                await update.message.reply_text("✅ Batch completed. Restarting in 30s...")
                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Loop error: {e}")
                await update.message.reply_text(f"❌ Error: {e}
Retrying in 60s...")
                await asyncio.sleep(60)

        elapsed = time.time() - ACTIVE_TASKS[user_id]['start_time']
        STATS[user_id]['total_runs'] += 1
        STATS[user_id]['last_run'] = datetime.now().isoformat()
        save_stats()

        await update.message.reply_text(
            f"🎉 **Task Completed!**

"
            f"✅ Invited: {ACTIVE_TASKS[user_id]['invited_count']}
"
            f"💬 DMs: {ACTIVE_TASKS[user_id]['dm_count']}
"
            f"❌ Failed: {ACTIVE_TASKS[user_id]['failed_count']}
"
            f"⏱ Time: {int(elapsed//60)}m {int(elapsed%60)}s"
        )

        if user_id in ACTIVE_TASKS:
            del ACTIVE_TASKS[user_id]
        await client.disconnect()

    except Exception as e:
        logger.error(f"Task error: {e}")
        if user_id in ACTIVE_TASKS:
            del ACTIVE_TASKS[user_id]

# ---------------- BOT COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton('🚀 Start Task'), KeyboardButton('📊 Statistics')],
        [KeyboardButton('❓ Help'), KeyboardButton('🔄 Reset')]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "👋 **Welcome to Telegram Invite Bot!**

"
        "✨ **Features:**
"
        "• 🔒 Device spoofing for security
"
        "• 💾 Session persistence
"
        "• 🛡 Smart flood protection
"
        "• 📊 Live dashboard

"
        "Choose an option or use /run to start:",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **Commands:**

"
        "/run - Start new task
"
        "/rerun - Repeat last task
"
        "/pause - Pause task
"
        "/resume - Resume task
"
        "/stop - Stop task
"
        "/stats - View statistics
"
        "/clear - Clear history
"
        "/reset - Reset session
"
        "/cancel - Cancel operation

"
        "⚠️ **Important:**
"
        "• Wait 10-15 minutes between login attempts
"
        "• Don't share OTP codes
"
        "• Use /reset if you get security errors"
    )
    await update.message.reply_text(help_text)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    init_user_stats(user_id)
    
    stats = STATS[user_id]
    active = user_id in ACTIVE_TASKS
    
    stats_text = (
        f"📊 **Statistics**

"
        f"✅ Invited: {stats['total_invited']}
"
        f"💬 DMs: {stats['total_dms_sent']}
"
        f"❌ Failed: {stats['total_failed']}
"
        f"🔄 Runs: {stats['total_runs']}
"
        f"📍 Status: {'🟢 Running' if active else '⚪ Idle'}"
    )
    
    if active:
        task = ACTIVE_TASKS[user_id]
        runtime = int(time.time() - task['start_time'])
        stats_text += (
            f"

**Current Task:**
"
            f"✅ Invited: {task['invited_count']}
"
            f"💬 DMs: {task['dm_count']}
"
            f"❌ Failed: {task['failed_count']}
"
            f"⏱ Runtime: {runtime//60}m {runtime%60}s"
        )
    
    await update.message.reply_text(stats_text)

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = True
    await update.message.reply_text("⏸ **Task Paused**
Use /resume to continue")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = False
    await update.message.reply_text("▶️ **Task Resumed**")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task.")
        return
    
    ACTIVE_TASKS[user_id]['running'] = False
    await update.message.reply_text("🛑 **Task Stopping...**")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    files = [f'sent_{user_id}.txt', f'invited_{user_id}.txt']
    cleared = 0
    
    for f in files:
        try:
            if os.path.exists(f):
                os.remove(f)
                cleared += 1
        except Exception as e:
            logger.error(f"Error removing {f}: {e}")
    
    await update.message.reply_text(f"🗑 Cleared {cleared} files")

async def reset_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    session_name = f'session_{user_id}'
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("❌ Stop task first with /stop")
        return
    
    removed = False
    if os.path.exists(f'{session_name}.session'):
        try:
            os.remove(f'{session_name}.session')
            removed = True
        except Exception as e:
            logger.error(f"Error: {e}")
    
    if removed:
        await update.message.reply_text(
            "🔄 **Session reset!**

"
            "⚠️ **IMPORTANT:** Wait 10-15 minutes before trying /run again.
"
            "This prevents Telegram security blocks."
        )
    else:
        await update.message.reply_text("❌ No session to reset")

async def rerun_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("❌ Task already running! Use /stop first")
        return
    
    if user_id in USER_HISTORY:
        asyncio.create_task(invite_task(user_id, update, context))
    else:
        await update.message.reply_text("❌ No previous configuration. Use /run first.")

# ---------------- CONVERSATION HANDLERS ----------------
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("❌ Task already running! Use /stop first")
        return ConversationHandler.END
    
    # Check existing session
    if user_id in USER_HISTORY:
        data = USER_HISTORY[user_id]
        session_name = f'session_{user_id}'
        
        if os.path.exists(f'{session_name}.session'):
            try:
                api_id = int(data['api_id'])
                api_hash = data['api_hash']
                
                device_info = load_device_info(user_id)
                
                await update.message.reply_text("🔍 Checking session...")
                
                client = TelegramClient(
                    session_name, 
                    api_id, 
                    api_hash,
                    device_model=device_info['device_model'] if device_info else 'PC',
                    system_version=device_info['system_version'] if device_info else 'Windows 10',
                    app_version=device_info['app_version'] if device_info else '9.5.0'
                )
                
                await client.connect()
                
                if await client.is_user_authorized():
                    me = await client.get_me()
                    await client.disconnect()
                    
                    await update.message.reply_text(
                        f"✅ **Using saved session!**

"
                        f"👤 Logged in as: {me.first_name}
"
                        f"🚀 Starting task..."
                    )
                    
                    asyncio.create_task(invite_task(user_id, update, context))
                    return ConversationHandler.END
                else:
                    await client.disconnect()
                    os.remove(f'{session_name}.session')
                    await update.message.reply_text("❌ Session expired. Starting new login...")
            except Exception as e:
                logger.error(f"Session check failed: {e}")
                try:
                    if os.path.exists(f'{session_name}.session'):
                        os.remove(f'{session_name}.session')
                except:
                    pass
                await update.message.reply_text("❌ Session error. Starting fresh...")
    
    await update.message.reply_text(
        "📝 **Step 1/7: API ID**

"
        "🔑 Get credentials from: https://my.telegram.org

"
        "Enter API_ID:",
        reply_markup=ReplyKeyboardRemove()
    )
    return API_ID

async def api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        api_id_value = update.message.text.strip()
        int(api_id_value)
        context.user_data['api_id'] = api_id_value
        await update.message.reply_text("📝 **Step 2/7: API Hash**

Enter API_HASH:")
        return API_HASH
    except ValueError:
        await update.message.reply_text("❌ Invalid API ID. Enter numbers only:")
        return API_ID

async def api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['api_hash'] = update.message.text.strip()
    await update.message.reply_text(
        "📝 **Step 3/7: Phone Number**

"
        "📱 Enter phone with country code:
"
        "Example: +1234567890"
    )
    return PHONE

async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone_number = update.message.text.strip()
    if not phone_number.startswith('+'):
        await update.message.reply_text("❌ Phone must start with +. Try again:")
        return PHONE
    
    context.user_data['phone'] = phone_number
    user_id = str(update.effective_user.id)
    
    api_id = int(context.user_data['api_id'])
    api_hash = context.user_data['api_hash']
    session_name = f'session_{user_id}'
    
    # Clean old session
    if os.path.exists(f'{session_name}.session'):
        try:
            os.remove(f'{session_name}.session')
        except:
            pass
    
    # Generate unique device info
    device_info = generate_device_info(user_id)
    save_device_info(user_id, device_info)
    
    try:
        await update.message.reply_text(
            "🔗 **Connecting...**

"
            f"📱 Device: {device_info['device_model']}
"
            f"💻 System: {device_info['device_string']}

"
            "⚠️ **IMPORTANT:** Keep your OTP code private!"
        )
        
        client = TelegramClient(
            session_name, 
            api_id, 
            api_hash,
            device_model=device_info['device_model'],
            system_version=device_info['system_version'],
            app_version=device_info['app_version'],
            lang_code=device_info['lang_code'],
            system_lang_code=device_info['system_lang_code']
        )
        
        await client.connect()
        
        # Request code without forcing SMS
        sent_code = await client.send_code_request(phone_number, force_sms=False)
        
        TEMP_CLIENTS[user_id] = {
            'client': client,
            'phone_hash': sent_code.phone_code_hash
        }
        
        logger.info(f"OTP sent to {phone_number}")
        await update.message.reply_text(
            "📝 **Step 4/7: OTP Code**

"
            "📨 Code sent to your Telegram app.
"
            "Enter the 5-digit code:

"
            "🔒 DO NOT share this code with anyone!"
        )
        return OTP_CODE
    except Exception as e:
        logger.error(f"Error sending OTP: {e}")
        await update.message.reply_text(
            f"❌ Error: {e}

"
            "Check your credentials and try /run again"
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def otp_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    otp = update.message.text.strip()
    
    # Delete OTP message for security
    try:
        await update.message.delete()
        logger.info("OTP message deleted for security")
    except Exception as e:
        logger.warning(f"Failed to delete OTP message: {e}")
    
    if user_id not in TEMP_CLIENTS:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Session expired. Try /run again"
        )
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    phone_number = context.user_data['phone']
    
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔐 Verifying OTP..."
        )
        
        # Sign in with phone hash
        result = await client.sign_in(
            phone=phone_number,
            code=otp,
            phone_code_hash=TEMP_CLIENTS[user_id]['phone_hash']
        )
        
        # Check if user is authorized or needs 2FA
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"Login successful: {me.first_name}")
            
            # Small delay to ensure session is saved
            await asyncio.sleep(1)
            await client.disconnect()
            
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"✅ **Login Successful!**

"
                     f"👤 Account: {me.first_name} (@{me.username or 'N/A'})
"
                     f"💾 Session saved for future use!

"
                     f"📝 **Step 5/7: Source Group**

"
                     f"Enter source group:

"
                     f"**Examples:**
"
                     f"• @groupname
"
                     f"• https://t.me/groupname"
            )
            
            return SOURCE
        else:
            # Need 2FA password
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="📝 **Step 4.5/7: Two-Factor Authentication**

"
                     "🔐 Your account has 2FA enabled.
"
                     "Enter your 2FA password:"
            )
            return TWO_FA_PASSWORD
            
    except errors.SessionPasswordNeededError:
        # 2FA is required
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📝 **Step 4.5/7: Two-Factor Authentication**

"
                 "🔐 Your account has 2FA enabled.
"
                 "Enter your 2FA password:"
        )
        return TWO_FA_PASSWORD
    except errors.PhoneCodeInvalidError:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Invalid OTP code. Try /run again."
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"OTP verification error: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error: {e}

Try /run again"
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def two_fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    password = update.message.text.strip()
    
    # Delete password message for security
    try:
        await update.message.delete()
        logger.info("2FA password message deleted for security")
    except Exception as e:
        logger.warning(f"Failed to delete 2FA password message: {e}")
    
    if user_id not in TEMP_CLIENTS:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Session expired. Try /run again"
        )
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔐 Verifying 2FA password..."
        )
        
        # Sign in with 2FA password
        await client.sign_in(password=password)
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"2FA login successful: {me.first_name}")
            
            # Small delay to ensure session is saved
            await asyncio.sleep(1)
            await client.disconnect()
            
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"✅ **Login Successful!**

"
                     f"👤 Account: {me.first_name} (@{me.username or 'N/A'})
"
                     f"💾 Session saved for future use!

"
                     f"📝 **Step 5/7: Source Group**

"
                     f"Enter source group:

"
                     f"**Examples:**
"
                     f"• @groupname
"
                     f"• https://t.me/groupname"
            )
            
            return SOURCE
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Authentication failed. Try /run again."
            )
            if user_id in TEMP_CLIENTS:
                try:
                    await TEMP_CLIENTS[user_id]['client'].disconnect()
                except:
                    pass
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except errors.PasswordHashInvalidError:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Invalid 2FA password. Try /run again."
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"2FA verification error: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error: {e}

Try /run again"
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def source_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = update.message.text.strip()
    context.user_data['source_group'] = source
    
    await update.message.reply_text(
        "📝 **Step 6/7: Target Group**

"
        "Enter target group:

"
        "**Examples:**
"
        "• @grouptarget
"
        "• https://t.me/grouptarget"
    )
    return TARGET

async def target_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    context.user_data['target_group'] = target
    
    await update.message.reply_text(
        "📝 **Step 7/7: Invite Link**

"
        "Enter your group invite link:

"
        "**Example:**
"
        "• https://t.me/+AbCdEfGhIjKlMn"
    )
    return INVITE_LINK

async def invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    link = update.message.text.strip()
    
    # Save all user data
    USER_HISTORY[user_id] = {
        'api_id': context.user_data['api_id'],
        'api_hash': context.user_data['api_hash'],
        'phone': context.user_data['phone'],
        'source_group': context.user_data['source_group'],
        'target_group': context.user_data['target_group'],
        'invite_link': link,
        'settings': {
            'min_delay': 4.0,
            'max_delay': 10.0,
            'pause_time': 600,
            'max_invites': 0,
            'filter_online': False,
            'filter_verified': False,
            'skip_dm_on_fail': False,
            'custom_message': None
        }
    }
    save_user_history()
    
    await update.message.reply_text(
        "✅ **Configuration Saved!**

"
        "📋 **Summary:**
"
        f"📱 Phone: {context.user_data['phone']}
"
        f"📥 Source: {context.user_data['source_group']}
"
        f"📤 Target: {context.user_data['target_group']}
"
        f"🔗 Link: {link}

"
        "🚀 Starting task..."
    )
    
    # Start the invite task
    asyncio.create_task(invite_task(user_id, update, context))
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Clean up temp client if exists
    if user_id in TEMP_CLIENTS:
        try:
            await TEMP_CLIENTS[user_id]['client'].disconnect()
        except:
            pass
        del TEMP_CLIENTS[user_id]
    
    await update.message.reply_text(
        "❌ **Operation Cancelled**

"
        "Use /run to start again.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

# ---------------- MAIN FUNCTION ----------------
def run_flask():
    """Run Flask app in a separate thread"""
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

async def main():
    """Main function to run the bot"""
    logger.info("🚀 Starting Telegram Invite Bot...")
    
    # Start Flask in a separate thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"🌐 Flask dashboard started on port {PORT}")
    
    # Create bot application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('run', run_command),
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
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )
    
    # Add handlers
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(MessageHandler(filters.Regex('^❓ Help$'), help_command))
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(MessageHandler(filters.Regex('^📊 Statistics$'), stats_command))
    application.add_handler(CommandHandler('pause', pause_command))
    application.add_handler(CommandHandler('resume', resume_command))
    application.add_handler(CommandHandler('stop', stop_command))
    application.add_handler(CommandHandler('clear', clear_command))
    application.add_handler(CommandHandler('reset', reset_session))
    application.add_handler(MessageHandler(filters.Regex('^🔄 Reset$'), reset_session))
    application.add_handler(CommandHandler('rerun', rerun_command))
    
    logger.info("✅ Bot handlers registered")
    logger.info(f"📊 Dashboard: http://0.0.0.0:{PORT}")
    logger.info("🤖 Bot starting polling...")
    
    # Start bot
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    
    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
