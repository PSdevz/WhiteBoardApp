import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template
from flask_socketio import SocketIO, emit
import redis
import json
import os
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'a-very-secret-key-change-this'

# --- Redis Setup (Master → Backup Failover) ---
MASTER_HOST = os.environ.get("REDIS_MASTER", "192.168.64.5")
BACKUP_HOST = os.environ.get("REDIS_BACKUP", "192.168.64.16")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))

def connect_redis(host):
    try:
        client = redis.StrictRedis(host=host, port=REDIS_PORT, decode_responses=True)
        client.ping()
        return client
    except:
        return None

# Try connecting to master, then backup
r_master = connect_redis(MASTER_HOST)
r_backup = connect_redis(BACKUP_HOST)

if r_master:
    print(f"Connected to MASTER Redis at {MASTER_HOST}")
    r = r_master
elif r_backup:
    print(f"Connected to BACKUP Redis at {BACKUP_HOST}")
    r = r_backup
else:
    print("❌ No Redis server available — running without Redis")
    r = None

socketio = SocketIO(app, cors_allowed_origins="*")
STATE_KEY = "whiteboard_state"

# --- Safe Redis commands with automatic failover ---
def safe_redis_command(cmd, *args, **kwargs):
    global r, r_master, r_backup
    if not r:
        return None
    try:
        return getattr(r, cmd)(*args, **kwargs)
    except:
        # Try switching to the other Redis if current fails
        for candidate in [r_master, r_backup]:
            if candidate and candidate != r:
                try:
                    candidate.ping()
                    r = candidate
                    print(f"Switched Redis client to {r}")
                    return getattr(r, cmd)(*args, **kwargs)
                except:
                    continue
        return None

# --- State ---
def save_state(data):
    safe_redis_command('rpush', STATE_KEY, json.dumps(data))

def load_state():
    items = safe_redis_command('lrange', STATE_KEY, 0, -1)
    if items:
        return [json.loads(x) for x in items]
    return []

# --- Routes ---
@app.route('/')
def index():
    return render_template('index.html')

# --- SocketIO Handlers ---
@socketio.on('connect')
def handle_connect():
    state = load_state()
    if state:
        emit('sync_state', state)

@socketio.on('draw')
def handle_draw(data):
    emit('draw', data, broadcast=True)
    if data.get('action') == 'clear':
        safe_redis_command('delete', STATE_KEY)
    else:
        save_state(data)
    safe_redis_command('publish', 'whiteboard_channel', json.dumps(data))

@socketio.on('clear_all')
def handle_clear_all():
    safe_redis_command('delete', STATE_KEY)
    clear_msg = json.dumps({'action': 'clear_all'})
    safe_redis_command('publish', 'whiteboard_channel', clear_msg)
    emit('clear_all', broadcast=True)

# --- Redis Listener (always active, auto-reconnect) ---
def redis_listener():
    global r
    while True:
        if not r:
            print("Waiting for Redis to become available...")
            r_master_try = connect_redis(MASTER_HOST)
            r_backup_try = connect_redis(BACKUP_HOST)
            r = r_master_try or r_backup_try
            if r:
                print(f"Reconnected to Redis at {r}")
            else:
                time.sleep(2)
                continue

        try:
            pubsub = r.pubsub()
            pubsub.subscribe('whiteboard_channel')
            print("Redis listener started...")
            for msg in pubsub.listen():
                if msg['type'] == 'message':
                    try:
                        data = json.loads(msg['data'])
                    except Exception:
                        continue
                    if data.get('action') == 'clear_all':
                        socketio.emit('clear_all')
                    else:
                        socketio.emit('draw', data)
        except Exception as e:
            print(f"Redis listener error: {e}")
            r = None
            time.sleep(2)

# --- Start server ---
if __name__ == "__main__":
    print("Starting server...")
    socketio.start_background_task(redis_listener)
    port = int(os.environ.get('PORT', 5001))
    socketio.run(app, host="0.0.0.0", port=port)