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
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, ConversationHandler, 
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from flask import Flask, render_template_string, jsonify, Response, request, abort
import queue

# ---------------- FLASK APP SETUP ----------------
app = Flask(__name__)
LOG_QUEUE = Queue(maxsize=1000)
USER_LOG_QUEUES = {}  # Separate queues for each user
DASHBOARD_TOKENS = {}  # Store secure tokens for dashboard access

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

class UserLogHandler(logging.Handler):
    """Handler to push logs to user-specific queues"""
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
        except queue.Full:
            try:
                USER_LOG_QUEUES[self.user_id].get_nowait()
                USER_LOG_QUEUES[self.user_id].put_nowait(log_entry)
            except:
                pass

# ---------------- LOGGING SETUP ----------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
TOKENS_FILE = 'dashboard_tokens.json'

# ---------------- DEVICE SPOOFING ----------------
def generate_device_info(user_id):
    """Generate unique device info for each user to avoid detection"""
    device_models = [
        'Samsung Galaxy S21', 'iPhone 13 Pro', 'Google Pixel 6', 'OnePlus 9 Pro',
        'Xiaomi Mi 11', 'OPPO Find X3', 'Vivo X60', 'Realme GT',
        'Motorola Edge 20', 'Nokia G50', 'Sony Xperia 5 III', 'ASUS ROG Phone 5'
    ]
    
    android_versions = ['11', '12', '13', '14']
    ios_versions = ['15.0', '15.5', '16.0', '16.3', '17.0']
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
        
        devices[str(user_id)] = device_info
        
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
                return devices.get(str(user_id))
    except Exception as e:
        logger.error(f"Error loading device info: {e}")
    return None

# ---------------- DASHBOARD TOKEN MANAGEMENT ----------------
def load_tokens():
    """Load dashboard tokens"""
    try:
        if os.path.exists(TOKENS_FILE):
            with open(TOKENS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading tokens: {e}")
    return {}

def save_tokens():
    """Save dashboard tokens"""
    try:
        with open(TOKENS_FILE, 'w') as f:
            json.dump(DASHBOARD_TOKENS, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving tokens: {e}")

def generate_dashboard_token(user_id):
    """Generate secure token for dashboard access"""
    token = secrets.token_urlsafe(32)
    DASHBOARD_TOKENS[token] = {
        'user_id': str(user_id),
        'created': datetime.now().isoformat(),
        'last_accessed': datetime.now().isoformat()
    }
    save_tokens()
    return token

def get_user_from_token(token):
    """Get user ID from token"""
    if token in DASHBOARD_TOKENS:
        DASHBOARD_TOKENS[token]['last_accessed'] = datetime.now().isoformat()
        save_tokens()
        return DASHBOARD_TOKENS[token]['user_id']
    return None

# Load existing tokens
DASHBOARD_TOKENS = load_tokens()

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

# ---------------- ENHANCED HTML TEMPLATE ----------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Telegram Invite Bot - Private Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta charset="UTF-8">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚀</text></svg>">
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
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.15);
            margin-bottom: 25px;
            text-align: center;
            position: relative;
            overflow: hidden;
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
        
        .header p {
            color: #6b7280;
            margin: 10px 0;
            font-size: 1.1em;
        }
        
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
            0%, 100% { 
                opacity: 1;
                transform: scale(1);
            }
            50% { 
                opacity: 0.85;
                transform: scale(1.05);
            }
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
        }
        
        .info-banner h3 {
            margin-bottom: 10px;
            font-size: 1.2em;
        }
        
        .info-banner p {
            opacity: 0.9;
            line-height: 1.6;
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
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            position: relative;
            overflow: hidden;
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
        
        .stat-card:hover::before {
            transform: scaleX(1);
        }
        
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
        
        .stat-card .icon {
            font-size: 2.5em;
            margin-bottom: 15px;
            opacity: 0.8;
        }
        
        .stat-card .subtext {
            color: #9ca3af;
            font-size: 0.85em;
            margin-top: 10px;
        }
        
        .logs-container {
            background: #1e293b;
            border-radius: 20px;
            padding: 25px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-height: 650px;
            overflow-y: auto;
        }
        
        .logs-container::-webkit-scrollbar {
            width: 10px;
        }
        
        .logs-container::-webkit-scrollbar-track {
            background: #0f172a;
            border-radius: 10px;
        }
        
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
            position: relative;
            border-left: 4px solid transparent;
        }
        
        @keyframes slideIn {
            from { 
                opacity: 0; 
                transform: translateX(-20px);
            }
            to { 
                opacity: 1; 
                transform: translateX(0);
            }
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
        
        .log-time {
            color: #cbd5e1;
            margin-right: 12px;
            font-weight: 700;
        }
        
        .button-group {
            display: flex;
            gap: 10px;
        }
        
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
        
        .footer p {
            color: #6b7280;
            margin: 5px 0;
        }
        
        @media (max-width: 768px) {
            .header h1 {
                font-size: 2em;
            }
            
            .grid {
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                gap: 15px;
            }
            
            .stat-card .value {
                font-size: 2em;
            }
            
            .logs-container {
                max-height: 400px;
            }
            
            .button-group {
                flex-direction: column;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚀 Telegram Invite Bot</h1>
            <p>Private Performance Dashboard</p>
            <a href="https://t.me/NY_BOTS" target="_blank" class="credit-badge">
                ⚡ Developed by @NY_BOTS
            </a>
            <br>
            <span id="status-badge" class="status-badge status-idle">● IDLE</span>
        </div>

        <div class="info-banner">
            <h3>🛡️ Safe & Secure Member Adding</h3>
            <p>
                ✅ This bot follows Telegram's guidelines to protect your account<br>
                ⚠️ Member adding is intentionally slow (4-10 seconds delay) to prevent bans<br>
                🔐 Uses device spoofing and smart flood protection for maximum safety<br>
                💡 Your account safety is our priority - patience ensures success!
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
            <p><strong>🔐 Security Notice:</strong> This is your private dashboard. Do not share this URL.</p>
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
"""

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
            body {
                font-family: Arial, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                margin: 0;
                padding: 20px;
            }
            .container {
                background: white;
                padding: 40px;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                text-align: center;
                max-width: 500px;
            }
            h1 { color: #667eea; margin-bottom: 20px; }
            p { color: #6b7280; margin: 15px 0; line-height: 1.6; }
            .credit { 
                margin-top: 30px; 
                padding-top: 20px; 
                border-top: 2px solid #e5e7eb;
            }
            a {
                color: #667eea;
                text-decoration: none;
                font-weight: bold;
            }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 Telegram Invite Bot</h1>
            <p>Welcome! To access your private dashboard, please start the bot on Telegram first.</p>
            <p>The bot will provide you with your personal secure dashboard link.</p>
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
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/status/<token>')
def api_status(token):
    user_id = get_user_from_token(token)
    if not user_id:
        abort(403)
    
    total_invited = 0
    total_dms = 0
    total_failed = 0
    active = 0

    if user_id in ACTIVE_TASKS:
        task = ACTIVE_TASKS[user_id]
        total_invited = task.get('invited_count', 0)
        total_dms = task.get('dm_count', 0)
        total_failed = task.get('failed_count', 0)
        active = 1

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
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'active_tasks': len(ACTIVE_TASKS),
        'timestamp': datetime.now().isoformat()
    })

# ---------------- HELPER FUNCTIONS ----------------
def log_to_user(user_id, level, message):
    """Send log to user's queue and chat"""
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

def clean_otp_code(otp_text):
    """Clean OTP code by removing spaces and non-numeric characters"""
    cleaned = re.sub(r'[^0-9]', '', otp_text)
    return cleaned

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
            f.write(f"{line}\n")
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
    if str(user_id) not in USER_HISTORY:
        return None
    settings = USER_HISTORY[str(user_id)].get('settings', {})
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
    if str(user_id) not in STATS:
        STATS[str(user_id)] = {
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

async def send_chat_log(update, message, context=None):
    """Send log message to user's chat"""
    try:
        if context:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=message)
        else:
            await update.message.reply_text(message)
    except Exception as e:
        logger.error(f"Error sending chat log: {e}")

async def invite_task(user_id, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    try:
        data = USER_HISTORY.get(str(user_id))
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

        ACTIVE_TASKS[str(user_id)] = {
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
            log_to_user(user_id, 'INFO', f"✓ Using device: {device_info['device_model']}")

        client = TelegramClient(session_name, api_id, api_hash)
        
        try:
            await client.connect()
            
            if not await client.is_user_authorized():
                await update.message.reply_text("❌ Session expired. Please use /run to login again.")
                if str(user_id) in ACTIVE_TASKS:
                    del ACTIVE_TASKS[str(user_id)]
                await client.disconnect()
                return
            
            me = await client.get_me()
            log_to_user(user_id, 'INFO', f"✓ Logged in as: {me.first_name}")
            await send_chat_log(update, f"✅ Logged in as: {me.first_name} (@{me.username or 'N/A'})", context)
        except Exception as e:
            logger.error(f"❌ Login failed: {e}")
            await update.message.reply_text(f"❌ Login failed: {e}")
            if str(user_id) in ACTIVE_TASKS:
                del ACTIVE_TASKS[str(user_id)]
            await client.disconnect()
            return

        while ACTIVE_TASKS.get(str(user_id), {}).get('running', False):
            try:
                already_sent = load_set(sent_file)
                already_invited = load_set(invited_file)

                target_entity = await client.get_entity(target_group)
                participants = await client.get_participants(source_group)
                
                log_to_user(user_id, 'INFO', f"🚀 Task started | {len(participants)} members found")
                await send_chat_log(update, 
                    f"🚀 Task Started Successfully!\n\n"
                    f"📊 Members Found: {len(participants)}\n"
                    f"⏱ Delay: {min_delay}-{max_delay}s (Safe Mode)\n"
                    f"📱 Device: {device_info['device_model'] if device_info else 'Default'}\n\n"
                    f"💡 Use /pause, /resume, or /stop to control", context)

                member_count = 0
                for user in participants:
                    if not ACTIVE_TASKS.get(str(user_id), {}).get('running', False):
                        break

                    while ACTIVE_TASKS.get(str(user_id), {}).get('paused', False):
                        await asyncio.sleep(2)

                    uid = str(getattr(user, 'id', ''))
                    if not uid or uid in already_invited or uid in already_sent:
                        continue
                    
                    if getattr(user, 'bot', False) or getattr(user, 'is_self', False):
                        continue

                    first_name = getattr(user, 'first_name', 'User') or 'User'
                    username = getattr(user, 'username', '')
                    member_count += 1

                    invited_ok, info = await try_invite(client, target_entity, user)
                    
                    if invited_ok:
                        log_to_user(user_id, 'INFO', f"✅ [INVITED] {first_name} (@{username if username else uid})")
                        if member_count % 5 == 0:
                            await send_chat_log(update, 
                                f"✅ Progress Update:\n"
                                f"Invited: {ACTIVE_TASKS[str(user_id)]['invited_count'] + 1}\n"
                                f"DMs: {ACTIVE_TASKS[str(user_id)]['dm_count']}\n"
                                f"Failed: {ACTIVE_TASKS[str(user_id)]['failed_count']}", context)
                        
                        append_line(invited_file, uid)
                        already_invited.add(uid)
                        ACTIVE_TASKS[str(user_id)]['invited_count'] += 1
                        STATS[str(user_id)]['total_invited'] += 1
                        save_stats()
                        
                        await asyncio.sleep(random.uniform(min_delay, max_delay))
                        continue

                    if info and info.startswith('floodwait'):
                        try:
                            wait_time = int(info.split(':')[1])
                            log_to_user(user_id, 'WARNING', f"⚠️ FloodWait: {wait_time}s - Waiting...")
                            await send_chat_log(update, f"⚠️ FloodWait! Waiting {wait_time}s...", context)
                            await asyncio.sleep(wait_time + 5)
                            continue
                        except:
                            await asyncio.sleep(pause_time)
                            continue
                    
                    if info == 'peerflood':
                        log_to_user(user_id, 'WARNING', f"⚠️ PeerFlood - Pausing {pause_time//60} min")
                        await send_chat_log(update, f"⚠️ PeerFlood! Pausing {pause_time//60} minutes for safety...", context)
                        await asyncio.sleep(pause_time)
                        continue

                    if not settings['skip_dm_on_fail'] and uid not in already_sent:
                        try:
                            custom_msg = settings.get('custom_message')
                            if custom_msg:
                                text = custom_msg.replace('{name}', first_name).replace('{link}', invite_link)
                            else:
                                text = f"Hi {first_name}! Join us: {invite_link}"
                            
                            await client.send_message(user.id, text)
                            log_to_user(user_id, 'INFO', f"📨 [DM SENT] {first_name}")
                            append_line(sent_file, uid)
                            already_sent.add(uid)
                            ACTIVE_TASKS[str(user_id)]['dm_count'] += 1
                            STATS[str(user_id)]['total_dms_sent'] += 1
                            save_stats()
                            
                            await asyncio.sleep(random.uniform(min_delay, max_delay))
                        except Exception as e:
                            log_to_user(user_id, 'ERROR', f"❌ DM failed: {type(e).__name__}")
                            ACTIVE_TASKS[str(user_id)]['failed_count'] += 1
                            STATS[str(user_id)]['total_failed'] += 1
                    else:
                        ACTIVE_TASKS[str(user_id)]['failed_count'] += 1
                        STATS[str(user_id)]['total_failed'] += 1

                log_to_user(user_id, 'INFO', "✓ Batch completed. Restarting in 30s...")
                await send_chat_log(update, "✅ Batch completed. Restarting in 30 seconds...", context)
                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"❌ Loop error: {e}")
                log_to_user(user_id, 'ERROR', f"❌ Error: {type(e).__name__}")
                await send_chat_log(update, f"❌ Error: {type(e).__name__}\nRetrying in 60s...", context)
                await asyncio.sleep(60)

        elapsed = time.time() - ACTIVE_TASKS[str(user_id)]['start_time']
        STATS[str(user_id)]['total_runs'] += 1
        STATS[str(user_id)]['last_run'] = datetime.now().isoformat()
        save_stats()

        final_msg = (
            f"🎉 Task Completed!\n\n"
            f"✅ Invited: {ACTIVE_TASKS[str(user_id)]['invited_count']}\n"
            f"📨 DMs: {ACTIVE_TASKS[str(user_id)]['dm_count']}\n"
            f"❌ Failed: {ACTIVE_TASKS[str(user_id)]['failed_count']}\n"
            f"⏱ Time: {int(elapsed//60)}m {int(elapsed%60)}s\n\n"
            f"⚡ Bot by @NY_BOTS"
        )
        await send_chat_log(update, final_msg, context)
        log_to_user(user_id, 'INFO', "🎉 Task completed successfully!")

        if str(user_id) in ACTIVE_TASKS:
            del ACTIVE_TASKS[str(user_id)]
        await client.disconnect()

    except Exception as e:
        logger.error(f"❌ Task error: {e}")
        if str(user_id) in ACTIVE_TASKS:
            del ACTIVE_TASKS[str(user_id)]

# ---------------- BOT COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Generate dashboard token if not exists
    token = None
    for t, data in DASHBOARD_TOKENS.items():
        if data['user_id'] == user_id:
            token = t
            break
    
    if not token:
        token = generate_dashboard_token(user_id)
    
    dashboard_url = f"https://uploader-bot-ny-1twx.onrender.com/dashboard/{token}"  # Replace with your actual URL
    
    keyboard = [
        [InlineKeyboardButton('🚀 Start New Task', callback_data='start_task')],
        [InlineKeyboardButton('🔄 Restart Last Task', callback_data='rerun_task')],
        [InlineKeyboardButton('📊 View Statistics', callback_data='view_stats')],
        [InlineKeyboardButton('🌐 Open Dashboard', url=dashboard_url)],
        [InlineKeyboardButton('❓ Help', callback_data='show_help')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "🤖 <b>Welcome to Telegram Invite Bot!</b>\n\n"
        "✨ <b>Premium Features:</b>\n"
        "• 🔐 Advanced device spoofing\n"
        "• 💾 Persistent session storage\n"
        "• 🛡️ Smart flood protection\n"
        "• 📊 Real-time dashboard\n"
        "• 🎯 Intelligent member targeting\n"
        "• 🔒 Account safety guaranteed\n\n"
        "⚠️ <b>Important:</b> Member adding is intentionally slow (4-10s delay) "
        "to prevent account bans and follow Telegram guidelines.\n\n"
        "⚡ <i>Developed by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup,
        parse_mode='HTML',
        disable_web_page_preview=True
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'start_task':
        await query.message.reply_text("Starting new task setup...")
        return await run_command(update, context)
    elif query.data == 'rerun_task':
        return await rerun_command(update, context)
    elif query.data == 'view_stats':
        return await stats_command(update, context)
    elif query.data == 'show_help':
        return await help_command(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 <b>Available Commands:</b>\n\n"
        "🚀 /run - Start new task\n"
        "🔄 /rerun - Repeat last task\n"
        "⏸ /pause - Pause running task\n"
        "▶️ /resume - Resume paused task\n"
        "⏹ /stop - Stop task completely\n"
        "📊 /stats - View statistics\n"
        "🗑 /clear - Clear history files\n"
        "🔄 /reset - Reset session\n"
        "❌ /cancel - Cancel operation\n\n"
        "⚠️ <b>Important Notes:</b>\n"
        "• Wait 10-15 minutes between login attempts\n"
        "• NEVER share OTP codes with anyone\n"
        "• Use /reset if you get security errors\n"
        "• OTP format: You can enter spaces (e.g., '1 2 3 4 5')\n"
        "• Slow speed = Safe account (follows Telegram rules)\n\n"
        "💡 <b>Tip:</b> View live dashboard from the main menu!\n\n"
        "⚡ <i>Bot by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>"
    )
    
    if update.callback_query:
        await update.callback_query.message.reply_text(help_text, parse_mode='HTML', disable_web_page_preview=True)
    else:
        await update.message.reply_text(help_text, parse_mode='HTML', disable_web_page_preview=True)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id if update.message else update.callback_query.from_user.id)
    init_user_stats(user_id)
    
    stats = STATS[user_id]
    active = user_id in ACTIVE_TASKS
    
    stats_text = (
        f"📊 <b>Your Statistics</b>\n\n"
        f"✅ Total Invited: <b>{stats['total_invited']}</b>\n"
        f"📨 Total DMs: <b>{stats['total_dms_sent']}</b>\n"
        f"❌ Total Failed: <b>{stats['total_failed']}</b>\n"
        f"🔄 Total Runs: <b>{stats['total_runs']}</b>\n"
        f"🔴 Status: <b>{'🟢 Running' if active else '⚫ Idle'}</b>"
    )
    
    if active:
        task = ACTIVE_TASKS[user_id]
        runtime = int(time.time() - task['start_time'])
        stats_text += (
            f"\n\n🔥 <b>Current Task:</b>\n"
            f"✅ Invited: <b>{task['invited_count']}</b>\n"
            f"📨 DMs: <b>{task['dm_count']}</b>\n"
            f"❌ Failed: <b>{task['failed_count']}</b>\n"
            f"⏱ Runtime: <b>{runtime//60}m {runtime%60}s</b>\n"
            f"{'⏸ <b>PAUSED</b>' if task.get('paused') else '▶️ <b>RUNNING</b>'}"
        )
    
    stats_text += "\n\n⚡ <i>Bot by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>"
    
    if update.callback_query:
        await update.callback_query.message.reply_text(stats_text, parse_mode='HTML', disable_web_page_preview=True)
    else:
        await update.message.reply_text(stats_text, parse_mode='HTML', disable_web_page_preview=True)

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to pause.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = True
    log_to_user(user_id, 'WARNING', "⏸ Task paused by user")
    await update.message.reply_text("⏸ <b>Task Paused</b>\n\nUse /resume to continue", parse_mode='HTML')

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to resume.")
        return
    
    ACTIVE_TASKS[user_id]['paused'] = False
    log_to_user(user_id, 'INFO', "▶️ Task resumed by user")
    await update.message.reply_text("▶️ <b>Task Resumed</b>", parse_mode='HTML')

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in ACTIVE_TASKS:
        await update.message.reply_text("❌ No active task to stop.")
        return
    
    ACTIVE_TASKS[user_id]['running'] = False
    log_to_user(user_id, 'WARNING', "⏹ Task stopped by user")
    await update.message.reply_text("⏹ <b>Task Stopping...</b> Please wait.", parse_mode='HTML')

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
    
    logger.info(f"🗑 User {user_id} cleared {cleared} files")
    await update.message.reply_text(
        f"🗑 <b>Cleared {cleared} history files</b>\n\n✅ Fresh start ready!",
        parse_mode='HTML'
    )

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
            logger.info(f"🔄 Session reset for user {user_id}")
        except Exception as e:
            logger.error(f"Error: {e}")
    
    if removed:
        await update.message.reply_text(
            "🔄 <b>Session Reset Complete!</b>\n\n"
            "⚠️ <b>IMPORTANT:</b> Wait 10-15 minutes before using /run again.\n"
            "This prevents Telegram security blocks.\n\n"
            "✅ You can login fresh after the wait period.",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text("ℹ️ No session file found to reset")

# ---------------- CONVERSATION HANDLERS ----------------
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle both direct command and callback query
    if update.callback_query:
        user_id = str(update.callback_query.from_user.id)
        message = update.callback_query.message
    else:
        user_id = str(update.effective_user.id)
        message = update.message
    
    if user_id in ACTIVE_TASKS:
        await message.reply_text("❌ Task already running! Use /stop first")
        return ConversationHandler.END
    
    if user_id in USER_HISTORY:
        data = USER_HISTORY[user_id]
        session_name = f'session_{user_id}'
        
        if os.path.exists(f'{session_name}.session'):
            try:
                api_id = int(data['api_id'])
                api_hash = data['api_hash']
                
                device_info = load_device_info(user_id)
                
                await message.reply_text("🔍 Checking saved session...")
                
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
                    
                    logger.info(f"✓ Using saved session for {me.first_name}")
                    await message.reply_text(
                        f"✅ <b>Using Saved Session!</b>\n\n"
                        f"👤 Logged in as: <b>{me.first_name}</b>\n"
                        f"🚀 Starting task now...\n\n"
                        f"⚡ <i>Bot by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>",
                        parse_mode='HTML',
                        disable_web_page_preview=True
                    )
                    
                    # Create a fake update object for invite_task
                    class FakeUpdate:
                        def __init__(self, msg):
                            self.message = msg
                            self.effective_chat = msg.chat
                    
                    fake_update = FakeUpdate(message)
                    asyncio.create_task(invite_task(user_id, fake_update, context))
                    return ConversationHandler.END
                else:
                    await client.disconnect()
                    os.remove(f'{session_name}.session')
                    logger.warning(f"⚠ Session expired for user {user_id}")
                    await message.reply_text("⚠️ Session expired. Starting new login...")
            except Exception as e:
                logger.error(f"Session check failed: {e}")
                try:
                    if os.path.exists(f'{session_name}.session'):
                        os.remove(f'{session_name}.session')
                except:
                    pass
                await message.reply_text("⚠️ Session error. Starting fresh login...")
    
    await message.reply_text(
        "🚀 <b>Step 1/7: API Credentials</b>\n\n"
        "📍 Get your credentials from:\n"
        "https://my.telegram.org\n\n"
        "🔢 Enter your <b>API_ID:</b>",
        parse_mode='HTML'
    )
    return API_ID

async def api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        api_id_value = update.message.text.strip()
        int(api_id_value)
        context.user_data['api_id'] = api_id_value
        logger.info(f"✓ API ID received from user {update.effective_user.id}")
        await update.message.reply_text(
            "✅ API ID saved!\n\n🔑 <b>Step 2/7: API Hash</b>\n\nEnter your <b>API_HASH:</b>",
            parse_mode='HTML'
        )
        return API_HASH
    except ValueError:
        await update.message.reply_text("❌ Invalid API ID. Must be numbers only.\n\nTry again:")
        return API_ID

async def api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['api_hash'] = update.message.text.strip()
    logger.info(f"✓ API Hash received from user {update.effective_user.id}")
    await update.message.reply_text(
        "✅ API Hash saved!\n\n"
        "📱 <b>Step 3/7: Phone Number</b>\n\n"
        "Enter your phone with country code:\n"
        "Example: +1234567890",
        parse_mode='HTML'
    )
    return PHONE

async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone_number = update.message.text.strip()
    if not phone_number.startswith('+'):
        await update.message.reply_text("❌ Phone must start with +\n\nTry again:")
        return PHONE
    
    context.user_data['phone'] = phone_number
    user_id = str(update.effective_user.id)
    
    api_id = int(context.user_data['api_id'])
    api_hash = context.user_data['api_hash']
    session_name = f'session_{user_id}'
    
    if os.path.exists(f'{session_name}.session'):
        try:
            os.remove(f'{session_name}.session')
        except:
            pass
    
    device_info = generate_device_info(user_id)
    save_device_info(user_id, device_info)
    
    try:
        await update.message.reply_text(
            "🔌 <b>Connecting to Telegram...</b>\n\n"
            f"📱 Device: <code>{device_info['device_model']}</code>\n"
            f"🖥 System: <code>{device_info['device_string']}</code>\n"
            f"📦 App: <code>{device_info['app_version']}</code>\n\n"
            "⏳ Please wait...",
            parse_mode='HTML'
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
        
        sent_code = await client.send_code_request(phone_number, force_sms=False)
        
        TEMP_CLIENTS[user_id] = {
            'client': client,
            'phone_hash': sent_code.phone_code_hash
        }
        
        logger.info(f"✓ OTP sent to {phone_number}")
        await update.message.reply_text(
            "✅ <b>OTP Code Sent!</b>\n\n"
            "📱 <b>Step 4/7: Verification Code</b>\n\n"
            "Check your Telegram app for a 5-digit code.\n\n"
            "🔢 Enter the code (spaces are OK):\n"
            "Examples:\n"
            "• 12345\n"
            "• 1 2 3 4 5\n\n"
            "⚠️ <b>NEVER share this code with anyone!</b>\n"
            "🔒 This message will be auto-deleted for security.",
            parse_mode='HTML'
        )
        return OTP_CODE
    except Exception as e:
        logger.error(f"❌ Error sending OTP: {e}")
        await update.message.reply_text(
            f"❌ <b>Error:</b> {e}\n\n"
            "Please check your credentials and try /run again.\n\n"
            "If error persists, wait 10-15 minutes and try again.",
            parse_mode='HTML'
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
    otp_raw = update.message.text.strip()
    
    try:
        await update.message.delete()
    except:
        pass
    
    otp = clean_otp_code(otp_raw)
    
    if not otp or len(otp) != 5:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Invalid OTP format. Must be 5 digits.\n\nYou entered: {len(otp)} digits\n\nTry again:"
        )
        return OTP_CODE
    
    if user_id not in TEMP_CLIENTS:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Session expired. Please use /run to start over."
        )
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    phone_number = context.user_data['phone']
    
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔐 Verifying OTP code..."
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
            
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🎉 <b>Login Successful!</b>\n\n"
                     f"👤 Account: <b>{me.first_name}</b>\n"
                     f"📱 Username: <b>@{me.username or 'N/A'}</b>\n"
                     f"💾 Session saved for future use!\n\n"
                     f"📍 <b>Step 5/7: Source Group</b>\n\n"
                     f"Enter the source group to scrape members from:\n\n"
                     f"Examples:\n"
                     f"• @groupusername\n"
                     f"• https://t.me/groupname\n"
                     f"• -100123456789 (group ID)",
                parse_mode='HTML'
            )
            
            return SOURCE
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Login failed. Authorization not granted.\n\nUse /run to try again."
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
            chat_id=update.effective_chat.id,
            text="🔐 <b>Step 4.5/7: Two-Factor Authentication</b>\n\n"
                 "Your account has 2FA enabled.\n\n"
                 "🔑 Enter your 2FA password:",
            parse_mode='HTML'
        )
        return TWO_FA_PASSWORD
    except Exception as e:
        logger.error(f"❌ OTP verification failed: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ <b>Error:</b> {str(e)}\n\n"
                 "Possible reasons:\n"
                 "• Wrong OTP code\n"
                 "• Code expired (request new one)\n"
                 "• Security block (wait 10-15 min)\n\n"
                 "Use /run to try again.",
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
    user_id = str(update.effective_user.id)
    password = update.message.text.strip()
    
    try:
        await update.message.delete()
    except:
        pass
    
    if user_id not in TEMP_CLIENTS:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Session expired. Use /run to start over."
        )
        return ConversationHandler.END
    
    client = TEMP_CLIENTS[user_id]['client']
    
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔐 Verifying 2FA password..."
        )
        
        await client.sign_in(password=password)
        
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info(f"✅ 2FA login successful: {me.first_name}")
            
            await client.disconnect()
            
            if user_id in TEMP_CLIENTS:
                del TEMP_CLIENTS[user_id]
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🎉 <b>Login Successful!</b>\n\n"
                     f"👤 Account: <b>{me.first_name}</b>\n"
                     f"📱 Username: <b>@{me.username or 'N/A'}</b>\n"
                     f"💾 Session saved!\n\n"
                     f"📍 <b>Step 5/7: Source Group</b>\n\n"
                     f"Enter source group:\n"
                     f"• @groupusername\n"
                     f"• https://t.me/groupname",
                parse_mode='HTML'
            )
            
            return SOURCE
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ 2FA verification failed.\n\nUse /run to try again."
            )
            if user_id in TEMP_CLIENTS:
                try:
                    await TEMP_CLIENTS[user_id]['client'].disconnect()
                except:
                    pass
                del TEMP_CLIENTS[user_id]
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"❌ 2FA failed: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Wrong 2FA password.\n\nUse /run to try again."
        )
        if user_id in TEMP_CLIENTS:
            try:
                await TEMP_CLIENTS[user_id]['client'].disconnect()
            except:
                pass
            del TEMP_CLIENTS[user_id]
        return ConversationHandler.END

async def source_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['source_group'] = update.message.text.strip()
    logger.info(f"✓ Source group set for user {update.effective_user.id}")
    await update.message.reply_text(
        "✅ Source group saved!\n\n"
        "🎯 <b>Step 6/7: Target Group</b>\n\n"
        "Enter the target group to add members to:\n\n"
        "Examples:\n"
        "• @targetgroup\n"
        "• https://t.me/targetgroup\n"
        "• -100987654321",
        parse_mode='HTML'
    )
    return TARGET

async def target_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['target_group'] = update.message.text.strip()
    logger.info(f"✓ Target group set for user {update.effective_user.id}")
    await update.message.reply_text(
        "✅ Target group saved!\n\n"
        "🔗 <b>Step 7/7: Invite Link</b>\n\n"
        "Enter the invite link for your target group:\n\n"
        "Example: https://t.me/+AbCdEfGhIjKl",
        parse_mode='HTML'
    )
    return INVITE_LINK

async def invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    invite_link_text = update.message.text.strip()
    
    context.user_data['invite_link'] = invite_link_text
    
    USER_HISTORY[user_id] = {
        'api_id': context.user_data['api_id'],
        'api_hash': context.user_data['api_hash'],
        'phone': context.user_data['phone'],
        'source_group': context.user_data['source_group'],
        'target_group': context.user_data['target_group'],
        'invite_link': invite_link_text,
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
    logger.info(f"✅ Setup complete for user {user_id}")
    
    # Generate dashboard link
    token = None
    for t, data in DASHBOARD_TOKENS.items():
        if data['user_id'] == user_id:
            token = t
            break
    
    if not token:
        token = generate_dashboard_token(user_id)
    
    dashboard_url = f"https://your-app-url.com/dashboard/{token}"  # Replace with actual URL
    
    await update.message.reply_text(
        "🎉 <b>Setup Complete!</b>\n\n"
        "✅ All configurations saved\n"
        "🚀 Starting task now...\n\n"
        "🌐 <b>Live Dashboard:</b>\n"
        f"<code>{dashboard_url}</code>\n\n"
        "💡 You can control the task with:\n"
        "⏸ /pause - Pause task\n"
        "▶️ /resume - Resume task\n"
        "⏹ /stop - Stop task\n"
        "📊 /stats - View progress\n\n"
        "⚡ <i>Bot by</i> <a href='https://t.me/NY_BOTS'>@NY_BOTS</a>",
        parse_mode='HTML',
        disable_web_page_preview=True
    )
    
    asyncio.create_task(invite_task(user_id, update, context))
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id in TEMP_CLIENTS:
        try:
            await TEMP_CLIENTS[user_id]['client'].disconnect()
        except:
            pass
        del TEMP_CLIENTS[user_id]
    
    logger.info(f"❌ Setup cancelled by user {user_id}")
    await update.message.reply_text(
        "❌ <b>Setup Cancelled</b>\n\n"
        "Use /run to start again anytime!",
        parse_mode='HTML'
    )
    return ConversationHandler.END

async def rerun_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle both direct command and callback query
    if update.callback_query:
        user_id = str(update.callback_query.from_user.id)
        message = update.callback_query.message
    else:
        user_id = str(update.effective_user.id)
        message = update.message
    
    if user_id in ACTIVE_TASKS:
        await message.reply_text("❌ Task already running! Use /stop first")
        return
    
    if user_id not in USER_HISTORY:
        await message.reply_text("❌ No previous configuration found.\n\nUse /run to setup first.")
        return
    
    session_name = f'session_{user_id}'
    if not os.path.exists(f'{session_name}.session'):
        await message.reply_text("❌ Session expired. Use /run to login again.")
        return
    
    logger.info(f"🔄 Rerun requested by user {user_id}")
    await message.reply_text("🔄 <b>Restarting previous task...</b>", parse_mode='HTML')
    
    # Create fake update for invite_task
    class FakeUpdate:
        def __init__(self, msg):
            self.message = msg
            self.effective_chat = msg.chat
    
    fake_update = FakeUpdate(message)
    asyncio.create_task(invite_task(user_id, fake_update, context))

# ---------------- MAIN FUNCTION ----------------
def main():
    """Start the bot and Flask server"""
    
    # Create bot application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation handler for setup
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('run', run_command),
            CallbackQueryHandler(button_callback, pattern='^start_task$')
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
    )
    
    # Add handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(CommandHandler('pause', pause_command))
    application.add_handler(CommandHandler('resume', resume_command))
    application.add_handler(CommandHandler('stop', stop_command))
    application.add_handler(CommandHandler('clear', clear_command))
    application.add_handler(CommandHandler('reset', reset_session))
    application.add_handler(CommandHandler('rerun', rerun_command))
    application.add_handler(conv_handler)
    
    # Callback query handler for inline buttons
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Start Flask in a separate thread
    def run_flask():
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
    
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    logger.info(f"🚀 Bot started successfully!")
    logger.info(f"🌐 Dashboard available at: http://0.0.0.0:{PORT}")
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
