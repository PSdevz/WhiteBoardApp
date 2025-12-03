import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template
from flask_socketio import SocketIO, emit
import redis
import json
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'a-very-secret-key-change-this'

# --- Redis Setup (Automatic Master → Backup Failover) ---
MASTER_HOST = os.environ.get("REDIS_MASTER", "192.168.64.5")
BACKUP_HOST = os.environ.get("REDIS_BACKUP", "192.168.64.16")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))

def connect_redis(host):
    """Try connecting to Redis at given host."""
    try:
        r = redis.StrictRedis(host=host, port=REDIS_PORT, decode_responses=True)
        r.ping()
        return r
    except:
        return None

# Try master first, fallback to backup
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

# --- Safe Redis commands with failover ---
def safe_redis_command(cmd, *args, **kwargs):
    global r, r_master, r_backup
    if not r:
        return None
    try:
        return getattr(r, cmd)(*args, **kwargs)
    except:
        # Try switching to the other Redis if current fails
        other = r_backup if r == r_master else r_master
        if other:
            try:
                other.ping()
                r = other
                return getattr(r, cmd)(*args, **kwargs)
            except:
                return None
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

# --- Redis listener ---
def redis_listener():
    if not r:
        print("Not starting Redis listener (no Redis connected)")
        return
    pubsub = r.pubsub()
    pubsub.subscribe('whiteboard_channel')
    print("Redis listener started...")
    for msg in pubsub.listen():
        if msg['type'] == 'message':
            data = json.loads(msg['data'])
            if data.get('action') == 'clear_all':
                socketio.emit('clear_all')
            else:
                socketio.emit('draw', data)

# --- Start server ---
if __name__ == "__main__":
    print("Starting server...")
    if r:
        socketio.start_background_task(redis_listener)
    port = int(os.environ.get('PORT', 5001))
    socketio.run(app, host="0.0.0.0", port=port)