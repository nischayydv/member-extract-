import asyncio
import os
import json
import random
import time
import logging
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
        .task-details {
            background: white;
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        .task-details h2 {
            color: #667eea;
            margin-bottom: 15px;
            font-size: 1.5em;
        }
        .task-info {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }
        .task-info-item {
            padding: 15px;
            background: linear-gradient(135deg, #f3f4f6 0%, #e5e7eb 100%);
            border-radius: 10px;
            border-left: 4px solid #667eea;
        }
        .task-info-item strong {
            color: #4b5563;
            display: block;
            margin-bottom: 5px;
            font-size: 0.9em;
        }
        .task-info-item span {
            color: #1f2937;
            font-size: 1.1em;
            font-weight: 600;
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
        .auto-scroll-btn:hover {
            transform: scale(1.05);
        }
        ::-webkit-scrollbar {
            width: 10px;
        }
        ::-webkit-scrollbar-track {
            background: #0f172a;
            border-radius: 10px;
        }
        ::-webkit-scrollbar-thumb {
            background: linear-gradient(135deg, #475569 0%, #334155 100%);
            border-radius: 10px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: linear-gradient(135deg, #64748b 0%, #475569 100%);
        }
        @media (max-width: 768px) {
            .header h1 {
                font-size: 1.8em;
            }
            .grid {
                grid-template-columns: repeat(2, 1fr);
            }
            .task-info {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Telegram Invite Bot</h1>
            <p>Real-Time Status Dashboard & Live Monitoring</p>
            <span id="status-badge" class="status-badge status-idle">IDLE</span>
        </div>

        <div class="grid">
            <div class="stat-card">
                <div class="icon">Active</div>
                <h3>Active Tasks</h3>
                <div class="value" id="active-tasks">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">Invited</div>
                <h3>Total Invited</h3>
                <div class="value" id="total-invited">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">DMs</div>
                <h3>DMs Sent</h3>
                <div class="value" id="total-dms">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">Failed</div>
                <h3>Failed</h3>
                <div class="value" id="total-failed">0</div>
            </div>
        </div>

        <div id="task-details-container"></div>

        <div class="logs-container">
            <div class="logs-header">
                <span>Live Logs Stream</span>
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

                    const container = document.getElementById('task-details-container');
                    if (data.tasks && data.tasks.length > 0) {
                        let html = '<div class="task-details"><h2>Active Tasks Details</h2>';
                        data.tasks.forEach((task, index) => {
                            html += `
                                <div style="margin-bottom: 20px; padding: 15px; background: #f9fafb; border-radius: 10px;">
                                    <h3 style="color: #667eea; margin-bottom: 10px;">Task #${index + 1} - User: ${task.user_id}</h3>
                                    <div class="task-info">
                                        <div class="task-info-item">
                                            <strong>Invited</strong>
                                            <span>${task.invited_count}</span>
                                        </div>
                                        <div class="task-info-item">
                                            <strong>DMs Sent</strong>
                                            <span>${task.dm_count}</span>
                                        </div>
                                        <div class="task-info-item">
                                            <strong>Failed</strong>
                                            <span>${task.failed_count}</span>
                                        </div>
                                        <div class="task-info-item">
                                            <strong>Runtime</strong>
                                            <span>${task.runtime}</span>
                                        </div>
                                        <div class="task-info-item">
                                            <strong>Status</strong>
                                            <span>${task.paused ? 'Paused' : 'Running'}</span>
                                        </div>
                                    </div>
                                </div>
                            `;
                        });
                        html += '</div>';
                        container.innerHTML = html;
                    } else {
                        container.innerHTML = '';
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

        // Initialize
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
    """Main dashboard page"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/status')
def api_status():
    """API endpoint for status"""
    total_invited = 0
    total_dms = 0
    total_failed = 0
    tasks_list = []

    for user_id, task in ACTIVE_TASKS.items():
        total_invited += task.get('invited_count', 0)
        total_dms += task.get('dm_count', 0)
        total_failed += task.get('failed_count', 0)
        
        runtime = int(time.time() - task.get('start_time', time.time()))
        tasks_list.append({
            'user_id': user_id,
            'invited_count': task.get('invited_count', 0),
            'dm_count': task.get('dm_count', 0),
            'failed_count': task.get('failed_count', 0),
            'paused': task.get('paused', False),
            'runtime': f"{runtime//60}m {runtime%60}s"
        })

    return jsonify({
        'active_tasks': len(ACTIVE_TASKS),
        'total_invited': total_invited,
        'total_dms': total_dms,
        'total_failed': total_failed,
        'tasks': tasks_list
    })

@app.route('/api/logs/stream')
def logs_stream():
    """SSE endpoint for live logs"""
    def generate():
        while True:
            try:
                log = LOG_QUEUE.get(timeout=30)
                yield f"data: {json.dumps(log)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'time': datetime.now().strftime('%H:%M:%S'), 'level': 'INFO', 'message': 'Waiting for logs...'})}\n\n"
            except Exception as e:
                logger.error(f"Stream error: {e}")
                break
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/health')
def health():
    """Health check endpoint for Render"""
    return jsonify({
        'status': 'healthy',
        'active_tasks': len(ACTIVE_TASKS),
        'timestamp': datetime.now().isoformat()
    })

# ---------------- HELPER FUNCTIONS ----------------
def save_user_history():
    """Save user history to file"""
    try:
        with open(USER_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(USER_HISTORY, f, indent=2, ensure_ascii=False)
        logger.info("User history saved")
    except Exception as e:
        logger.error(f"Error saving user history: {e}")

def save_stats():
    """Save statistics to file"""
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(STATS, f, indent=2, ensure_ascii=False)
        logger.info("Stats saved")
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
            await update.message.reply_text("No configuration found. Use /run first.")
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
                await update.message.reply_text("Session expired. Please use /run to login again.")
                if user_id in ACTIVE_TASKS:
                    del ACTIVE_TASKS[user_id]
                await client.disconnect()
                return
            
            me = await client.get_me()
            logger.info(f"User {user_id} logged in as: {me.first_name} (@{me.username or 'N/A'})")
            await update.message.reply_text(f"Logged in as: {me.first_name} (@{me.username or 'N/A'})")
        except Exception as e:
            logger.error(f"Login failed for user {user_id}: {e}")
            await update.message.reply_text(f"Login failed: {e}")
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
                
                logger.info(f"Task started for user {user_id} | {len(participants)} members in source")
                await update.message.reply_text(
                    f"Task Started\n\n"
                    f"Total members: {len(participants)}\n"
                    f"Delay: {min_delay}-{max_delay}s\n"
                    f"Source: {source_group}\n"
                    f"Target: {target_group}\n\n"
                    f"Use /pause to pause | /stop to stop"
                )

                # Process each user
                for user in participants:
                    # Check if task is stopped or paused
                    if not ACTIVE_TASKS.get(user_id, {}).get('running', False):
                        logger.info(f"Task stopped by user {user_id}")
                        await update.message.reply_text("Task stopped by user.")
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
                        logger.info(f"[INVITED] {uid} - {first_name} ({info if info else 'success'})")
                        append_line(invited_file, uid)
                        already_invited.add(uid)
                        ACTIVE_TASKS[user_id]['invited_count'] += 1
                        STATS[user_id]['total_invited'] += 1
                        save_stats()
                        
                        await asyncio.sleep(random.uniform(min_delay, max_delay))
                        continue

                    # Handle FloodWait
                    if info and info.startswith('floodwait'):
                        try:
                            wait_time = int(info.split(':')[1])
                            logger.warning(f"FloodWait detected. Pausing {wait_time} seconds...")
                            await update.message.reply_text(
                                f"FloodWait Detected!\nWaiting {wait_time} seconds..."
                            )
                            await asyncio.sleep(wait_time + 5)
                            continue
                        except Exception as e:
                            logger.error(f"Error parsing floodwait: {e}")
                            await asyncio.sleep(pause_time)
                            continue
                    
                    # Handle PeerFlood
                    if info == 'peerflood':
                        logger.warning(f"PeerFlood detected. Pausing {pause_time//60} minutes...")
                        await update.message.reply_text(
                            f"PeerFlood Detected!\nPausing for {pause_time//60} minutes..."
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
                            logger.info(f"[MESSAGED] {uid} - {first_name}")
                            append_line(sent_file, uid)
                            already_sent.add(uid)
                            ACTIVE_TASKS[user_id]['dm_count'] += 1
                            STATS[user_id]['total_dms_sent'] += 1
                            save_stats()
                            
                            await asyncio.sleep(random.uniform(min_delay, max_delay))
                        except errors.UserPrivacyRestrictedError:
                            logger.info(f"Can't DM {uid}: privacy settings.")
                            ACTIVE_TASKS[user_id]['failed_count'] += 1
                            STATS[user_id]['total_failed'] += 1
                        except errors.FloodWaitError as e:
                            logger.warning(f"FloodWait during DM. Pausing {e.seconds} seconds...")
                            await asyncio.sleep(e.seconds + 5)
                        except errors.PeerFloodError:
                            logger.warning("PeerFlood detected on send. Pausing...")
                            await asyncio.sleep(pause_time)
                        except Exception as e:
                            logger.error(f"Failed to DM {uid}: {type(e).__name__} {e}")
                            ACTIVE_TASKS[user_id]['failed_count'] += 1
                            STATS[user_id]['total_failed'] += 1
                    else:
                        ACTIVE_TASKS[user_id]['failed_count'] += 1
                        STATS[user_id]['total_failed'] += 1

                # Batch finished
                logger.info("Batch finished. Restarting loop in 30 seconds...")
                await update.message.reply_text("Batch completed. Restarting in 30 seconds...")
                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await update.message.reply_text(f"Error: {e}\nRestarting in 60 seconds...")
                await asyncio.sleep(60)

        # Task completion
        elapsed = time.time() - ACTIVE_TASKS[user_id]['start_time']
        STATS[user_id]['total_runs'] += 1
        STATS[user_id]['last_run'] = datetime.now().isoformat()
        save_stats()

        logger.info(f"Task completed for user {user_id}")
        await update.message.reply_text(
            f"Task Completed!\n\n"
            f"Invited: {ACTIVE_TASKS[user_id]['invited_count']}\n"
            f"DMs Sent: {ACTIVE_TASKS[user_id]['dm_count']}\n"
            f"Failed: {ACTIVE_TASKS[user_id]['failed_count']}\n"
            f"Time: {int(elapsed//60)}m {int(elapsed%60)}s\n\n"
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
            await update.message.reply_text(f"Task error: {e}")
        except:
            pass

# ---------------- BOT COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    keyboard = [
        [KeyboardButton('Start New Task'), KeyboardButton('Repeat Last Task')],
        [KeyboardButton('Statistics'), KeyboardButton('Settings')],
        [KeyboardButton('Help'), KeyboardButton('Dashboard')]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome_msg = (
        "Welcome to Telegram Invite Bot!\n\n"
        "Features:\n"
        "- Bulk invite members from source to target group\n"
        "- Smart DM fallback when invite fails\n"
        "- Intelligent flood protection\n"
        "- Auto-restart with loop support\n"
        "- Pause/Resume functionality\n"
        "- Live web dashboard with real-time logs\n"
        "- OTP and 2FA authentication support\n\n"
        "Choose an option below to get started!"
    )
    
    await update.message.reply_text(welcome_msg, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    help_text = (
        "Command Guide\n\n"
        "Main Commands:\n"
        "/run - Start new invite task (with OTP login)\n"
        "/rerun - Repeat last saved task\n"
        "/pause - Pause currently running task\n"
        "/resume - Resume paused task\n"
        "/stop - Stop running task completely\n"
        "/stats - View your statistics\n"
        "/clear - Clear invite/DM history files\n"
        "/cancel - Cancel current operation\n\n"
        "Web Dashboard:\n"
        "Access live logs and stats at your deployment URL\n\n"
        "Best Practices:\n"
        "- Keep delays between 4-10 seconds\n"
        "- Monitor for flood warnings\n"
        "- Use pause during high activity\n"
        "- Check dashboard for real-time status\n"
        "- Clear history periodically for fresh starts"
    )
    await update.message.reply_text(help_text)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Statistics command handler"""
    user_id = str(update.effective_user.id)
    init_user_stats(user_id)
    
    stats = STATS[user_id]
    active = user_id in ACTIVE_TASKS
    
    stats_text = (
        f"Your Statistics\n\n"
        f"Total Invited: {stats['total_invited']}\n"
        f"Total DMs: {stats['total_dms_sent']}\n"
        f"Total Failed: {stats['total_failed']}\n"
        f"Total Runs: {stats['total_runs']}\n"
        f"Last Run: {stats['last_run'][:16] if stats['last_run'] else 'Never'}\n"
        f"Status: {'Running' if active else 'Idle'}\n\n"
    )
    
    if active:
        task = ACTIVE_TASKS[user_id]
        runtime = int(time.time() - task['start_time'])
        stats_text += (
            f"Current Task Progress:\n"
            f"Invited: {task['invited_count']}\n"
            f"DMs: {task['dm_count']}\n"
            f"Failed: {task['failed_count']}\n"
            f"Runtime: {runtime//60}m {runtime%60}s\n"
            f"Paused: {'Yes' if task.get('paused') else 'No'}"
        )
    
    await update.message.reply_text(stats_text)

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause command handler"""
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("No active task to pause.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = True
    logger.info(f"Task paused by user {user_id}")
    await update.message.reply_text("Task Paused\n\nUse /resume to continue.")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume command handler"""
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("No active task to resume.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = False
    logger.info(f"Task resumed by user {user_id}")
    await update.message.reply_text("Task Resumed\n\nProcessing continues...")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop command handler"""
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("No active task to stop.")
        return
    
    ACTIVE_TASKS[user_id]['running'] = False
    logger.info(f"Task stopped by user {user_id}")
    await update.message.reply_text("Task Stopping...\n\nPlease wait...")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear history command handler"""
    user_id = str(update.effective_user.id)
    
    files = [f'sent_{user_id}.txt', f'invited_{user_id}.txt']
    cleared = []
    
    for f in files:
        try:
            if os.path.exists(f):
                os.remove(f)
                cleared.append(f)
        except Exception as e:
            logger.error(f"Error removing {f}: {e}")
    
    logger.info(f"History cleared for user {user_id}: {cleared}")
    await update.message.reply_text(
        f"History Cleared!\n\n"
        f"Removed: {len(cleared)} files\n"
        f"You can now start fresh with /run"
    )

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dashboard info command"""
    await update.message.reply_text(
        "Web Dashboard\n\n"
        "Access your live dashboard at:\n"
        f"http://localhost:{PORT} (local)\n\n"
        "or at your Render deployment URL\n\n"
        "Features:\n"
        "- Real-time logs streaming\n"
        "- Live task statistics\n"
        "- Active task monitoring\n"
        "- Auto-refresh every 2 seconds"
    )

# ---------------- CONVERSATION HANDLERS ----------------
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run command - start conversation"""
    user_id = str(update.effective_user.id)
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("A task is already running! Use /stop first.")
        return ConversationHandler.END
    
    logger.info(f"User {user_id} started setup process")
    await update.message.reply_text(
        "Step 1/7: API ID\n\n"
        "Get your API credentials from: https://my.telegram.org\n\n"
        "Enter your API_ID (numbers only):",
        reply_markup=ReplyKeyboardRemove()
    )
    return API_ID

async def api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle API ID input"""
    try:
        api_id_value = update.message.text.strip()
        int(api_id_value)
        context.user_data['api_id'] = api_id_value
        logger.info(f"API ID received from user")
        await update.message.reply_text(
            "Step 2/7: API Hash\n\n"
            "Enter your API_HASH:"
        )
        return API_HASH
    except ValueError:
        await update.message.reply_text("Invalid API ID. Please enter numbers only:")
        return API_ID

async def api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle API Hash input"""
    context.user_data['api_hash'] = update.message.text.strip()
    logger.info(f"API Hash received from user")
    await update.message.reply_text(
        "Step 3/7: Phone Number\n\n"
        "Enter your phone number with country code:\n"
        "Example: +1234567890"
    )
    return PHONE

async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle phone number input and send OTP"""
    phone_number = update.message.text.strip()
    if not phone_number.startswith('+'):
        await update.message.reply_text("Phone should start with + and country code. Try again:")
        return PHONE
    
    context.user_data['phone'] = phone_number
    user_id = str(update.effective_user.id)
    
    # Create temporary client for login
    api_id = int(context.user_data['api_id'])
    api_hash = context.user_data['api_hash']
    session_name = f'session_{user_id}'
    
    try:
        await update.message.reply_text("Connecting to Telegram...")
        client = TelegramClient(session_name, api_id, api_hash)
        await client.connect()
        
        # Send OTP
        await update.message.reply_text("Sending OTP code...")
        await client.send_code_request(phone_number)
        TEMP_CLIENTS[user_id] = client
        
        logger.info(f"OTP sent to {phone_number} for user {user_id}")
        await update.message.reply_text(
            "Step 4/7: OTP Code\n\n"
            "An OTP code has been sent to your Telegram account.\n"
            "Please check your Telegram messages and enter the code here:\n\n"
            "Format: 12345 (5-digit code)"
        )
        return OTP_CODE
    except Exception as e:
        logger.error(f"Error sending OTP: {e}")
        await update.message.reply_text(
            f"Error sending OTP: {e}\n\n"
            f"Please check your credentials and try again with /run"
        )
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
        await update.message.reply_text("Session expired. Please start again with /run")
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]
    phone_number = context.user_data['phone']
    
    try:
        await update.message.reply_text("Verifying OTP code...")
        await client.sign_in(phone_number, otp)
        
        # Check if logged in successfully
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"User {user_id} logged in successfully as {me.first_name}")
            await update.message.reply_text(
                f"Login Successful!\n\n"
                f"Logged in as: {me.first_name} (@{me.username or 'N/A'})\n"
                f"User ID: {me.id}\n\n"
                f"Step 5/7: Source Group\n\n"
                f"Enter source group username or link:\n"
                f"Examples:\n"
                f"- @groupname\n"
                f"- https://t.me/groupname\n"
                f"- t.me/groupname"
            )
            
            # Disconnect temp client
            await client.disconnect()
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            return SOURCE
        else:
            await update.message.reply_text("Login failed. Please try again with /run")
            await client.disconnect()
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except errors.SessionPasswordNeededError:
        # Two-factor authentication required
        logger.info(f"2FA required for user {user_id}")
        await update.message.reply_text(
            "Step 4.5/7: 2FA Password\n\n"
            "Your account has Two-Factor Authentication enabled.\n"
            "Please enter your 2FA password:"
        )
        return TWO_FA_PASSWORD
    except errors.PhoneCodeInvalidError:
        await update.message.reply_text("Invalid OTP code. Please try again:")
        return OTP_CODE
    except errors.PhoneCodeExpiredError:
        await update.message.reply_text(
            "OTP code expired.\n\n"
            "Please start again with /run to receive a new code."
        )
        await client.disconnect()
        if user_id in TEMP_CLIENTS:
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error during sign in: {e}")
        await update.message.reply_text(f"Error: {e}\n\nPlease try again with /run")
        await client.disconnect()
        if user_id in TEMP_CLIENTS:
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def two_fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 2FA password input"""
    user_id = str(update.effective_user.id)
    password = update.message.text.strip()
    
    if user_id not in TEMP_CLIENTS:
        await update.message.reply_text("Session expired. Please start again with /run")
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]
    
    try:
        await update.message.reply_text("Verifying 2FA password...")
        await client.sign_in(password=password)
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"User {user_id} logged in successfully with 2FA")
            await update.message.reply_text(
                f"Login Successful!\n\n"
                f"Logged in as: {me.first_name} (@{me.username or 'N/A'})\n"
                f"User ID: {me.id}\n\n"
                f"Step 5/7: Source Group\n\n"
                f"Enter source group username or link:\n"
                f"Examples:\n"
                f"- @groupname\n"
                f"- https://t.me/groupname"
            )
            
            await client.disconnect()
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            return SOURCE
        else:
            await update.message.reply_text("Login failed. Please try again with /run")
            await client.disconnect()
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except errors.PasswordHashInvalidError:
        await update.message.reply_text("Invalid 2FA password. Please try again:")
        return TWO_FA_PASSWORD
    except Exception as e:
        logger.error(f"Error with 2FA: {e}")
        await update.message.reply_text(f"Error: {e}\n\nPlease try again with /run")
        await client.disconnect()
        if user_id in TEMP_CLIENTS:
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle source group input"""
    context.user_data['source_group'] = update.message.text.strip()
    logger.info(f"Source group set: {context.user_data['source_group']}")
    await update.message.reply_text(
        "Step 6/7: Target Group\n\n"
        "Enter target group username or link:\n"
        "This is where members will be invited to."
    )
    return TARGET

async def target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle target group input"""
    context.user_data['target_group'] = update.message.text.strip()
    logger.info(f"Target group set: {context.user_data['target_group']}")
    await update.message.reply_text(
        "Step 7/7: Invite Link\n\n"
        "Enter the invite link for your target group:\n"
        "This will be sent in DMs when direct invite fails.\n\n"
        "Example: https://t.me/+abc123"
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

    keyboard = [[KeyboardButton('Main Menu'), KeyboardButton('Statistics')]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    logger.info(f"Configuration saved for user {user_id}")
    await update.message.reply_text(
        "Configuration Saved!\n\n"
        f"Source: {context.user_data['source_group']}\n"
        f"Target: {context.user_data['target_group']}\n"
        f"Link: {context.user_data['invite_link']}\n\n"
        f"Starting invite task...\n"
        f"Monitor progress on web dashboard!",
        reply_markup=reply_markup
    )
    
    # Start task
    asyncio.create_task(invite_task(user_id, update, context))
    return ConversationHandler.END

async def rerun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rerun last task"""
    user_id = str(update.effective_user.id)
    
    if user_id not in USER_HISTORY:
        await update.message.reply_text("No previous configuration found. Use /run first.")
        return
    
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text("A task is already running! Use /stop first.")
        return
    
    logger.info(f"Rerunning task for user {user_id}")
    await update.message.reply_text(
        "Rerunning Last Task...\n\n"
        "Using your previous configuration.\n"
        "Monitor progress on web dashboard!"
    )
    asyncio.create_task(invite_task(user_id, update, context))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation"""
    user_id = str(update.effective_user.id)
    
    # Clean up temp client if exists
    if user_id in TEMP_CLIENTS:
        try:
            await TEMP_CLIENTS[user_id].disconnect()
            logger.info(f"Disconnected temp client for user {user_id}")
        except:
            pass
        del TEMP_CLIENTS[user_id]
    
    keyboard = [[KeyboardButton('Main Menu')]]
    await update.message.reply_text(
        "Operation Cancelled\n\n"
        "Use /start to begin again.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle keyboard button presses"""
    text = update.message.text
    
    if text == 'Start New Task':
        return await run_command(update, context)
    elif text == 'Repeat Last Task':
        return await rerun(update, context)
    elif text == 'Statistics':
        return await stats_command(update, context)
    elif text == 'Help':
        return await help_command(update, context)
    elif text == 'Dashboard':
        return await dashboard_command(update, context)
    elif text == 'Main Menu':
        return await start(update, context)
    elif text == 'Settings':
        await update.message.reply_text(
            "Settings\n\n"
            "Current settings are managed in the code.\n"
            "Default delays: 4-10 seconds\n"
            "Pause time: 10 minutes on flood\n\n"
            "Contact developer for custom settings."
        )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Exception while handling update: {context.error}")

# ---------------- FLASK SERVER THREAD ----------------
def run_flask():
    """Run Flask server in separate thread"""
    logger.info(f"Starting Flask server on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, threaded=True, debug=False)

# ---------------- BOT SETUP ----------------
def main():
    """Main function to run the bot"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in environment variables!")
        print("ERROR: BOT_TOKEN not set!")
        print("Please set BOT_TOKEN in environment variables.")
        return

    try:
        # Start Flask server in background thread
        flask_thread = Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info(f"Web dashboard started on port {PORT}")
        
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

        application = Application.builder().token(BOT_TOKEN).build()
        
        application.add_handler(CommandHandler('start', start))
        application.add_handler(CommandHandler('help', help_command))
        application.add_handler(CommandHandler('rerun', rerun))
        application.add_handler(CommandHandler('stats', stats_command))
        application.add_handler(CommandHandler('pause', pause_command))
        application.add_handler(CommandHandler('resume', resume_command))
        application.add_handler(CommandHandler('stop', stop_command))
        application.add_handler(CommandHandler('clear', clear_command))
        application.add_handler(CommandHandler('dashboard', dashboard_command))
        application.add_handler(conv_handler)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))
        application.add_error_handler(error_handler)

        logger.info("Bot is starting...")
        logger.info(f"Web dashboard available at: http://localhost:{PORT}")
        logger.info("All handlers registered")
        
        print("\n" + "="*60)
        print("TELEGRAM INVITE BOT - FULLY OPERATIONAL")
        print("="*60)
        print(f"Bot Token: {'*' * 20}{BOT_TOKEN[-10:]}")
        print(f"Dashboard: http://localhost:{PORT}")
        print(f"Health Check: http://localhost:{PORT}/health")
        print(f"Status: RUNNING")
        print("="*60 + "\n")
        
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        print("\nBot stopped gracefully")
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"Failed to start bot: {e}")

if __name__ == '__main__':
    main()
