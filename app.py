import eventlet
# Must monkey_patch early for SocketIO with Eventlet
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
    except Exception:
        return None

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

# --- Merge state from old Redis to new Redis on failover/failback ---
def merge_state(old_client, new_client, state_key=STATE_KEY):
    try:
        old_state = old_client.lrange(state_key, 0, -1)
        if not old_state:
            return
        new_state = new_client.lrange(state_key, 0, -1) or []
        missing_items = [item for item in old_state if item not in new_state]
        if missing_items:
            new_client.rpush(state_key, *missing_items)
            print(f"✅ Merged {len(missing_items)} items from old Redis to new Redis.")
    except Exception as e:
        print(f"⚠️ Error merging state: {e}")

# --- Safe Redis command wrapper ---
def safe_redis_command(cmd, *args, **kwargs):
    global r, r_master, r_backup
    if not r:
        return None

    old_r = r
    try:
        return getattr(r, cmd)(*args, **kwargs)
    except Exception as e:
        print(f"Redis command failed on current host: {e}")
        candidates = [c for c in [r_master, r_backup] if c and c != r]
        for candidate in candidates:
            try:
                candidate.ping()
                r = candidate
                if r == r_master and old_r == r_backup:
                    merge_state(old_r, r)
                    socketio.emit('force_sync')
                print(f"--- 🚨 FAILOVER SUCCESS: Switched active Redis to {r.connection_pool.connection_kwargs.get('host','UNKNOWN')}")
                return getattr(r, cmd)(*args, **kwargs)
            except Exception:
                continue
        print("Redis command failed on all hosts. Skipping...")
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

# --- Redis Listener ---
def redis_listener():
    global r, r_master, r_backup
    while True:
        if not r:
            r_master = connect_redis(MASTER_HOST)
            r_backup = connect_redis(BACKUP_HOST)
            r = r_master or r_backup
            if r:
                socketio.emit('force_sync')
            else:
                time.sleep(2)
                continue
        try:
            pubsub = r.pubsub()
            pubsub.subscribe('whiteboard_channel')
            print("Redis listener started...")
            for msg in pubsub.listen():
                if msg['type'] != 'message':
                    continue
                try:
                    data = json.loads(msg['data'])
                except Exception:
                    continue
                if data.get('action') == 'clear_all':
                    socketio.emit('clear_all')
                else:
                    socketio.emit('draw', data)
        except Exception as e:
            print(f"Redis listener error: {e}. Retrying...")
            r = None
            time.sleep(2)

# --- Start server ---
if __name__ == "__main__":
    print("Starting server...")
    socketio.start_background_task(redis_listener)
    port = int(os.environ.get('PORT', 5001))
    socketio.run(app, host="0.0.0.0", port=port)