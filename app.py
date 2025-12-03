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

def connect_to_redis(host):
    client = redis.StrictRedis(host=host, port=REDIS_PORT, decode_responses=True)
    client.ping()
    return client

# Always try to connect to both
r_master = None
r_backup = None

try:
    r_master = connect_to_redis(MASTER_HOST)
    print(f"Connected to MASTER Redis at {MASTER_HOST}")
except Exception as e:
    print(f"MASTER Redis unavailable at startup: {e}")
    r_master = None

try:
    r_backup = connect_to_redis(BACKUP_HOST)
    print(f"Connected to BACKUP Redis at {BACKUP_HOST}")
except Exception as e:
    print(f"BACKUP Redis unavailable at startup: {e}")
    r_backup = None

if not r_master and not r_backup:
    print("❌ No Redis server available — running without Redis.")

# Use master first if available, else backup
r = r_master if r_master else r_backup

def safe_redis_command(command, *args, **kwargs):
    global r, r_master, r_backup

    # Attempt fail-back to master if possible
    if r_master and r != r_master:
        try:
            r_master.ping()
            r = r_master
        except Exception:
            pass

    # Try command on current primary first, then fallback clients
    clients = [c for c in (r, r_master, r_backup) if c and c not in [None, r]]
    if r:
        clients.insert(0, r)

    for client in clients:
        try:
            result = getattr(client, command)(*args, **kwargs)
            if r != client:
                r = client
            return result
        except Exception:
            continue

    print(f"Redis {command} failed on all clients.")
    return None

socketio = SocketIO(app, cors_allowed_origins="*")
STATE_KEY = "whiteboard_state"

def save_state(data):
    try:
        safe_redis_command('rpush', STATE_KEY, json.dumps(data))
    except Exception as e:
        print(f"Redis save_state error: {e}")

def load_state():
    try:
        items = safe_redis_command('lrange', STATE_KEY, 0, -1)
        if items:
            return [json.loads(x) for x in items]
    except Exception as e:
        print(f"Redis load_state error: {e}")
    return []

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    state = load_state()
    if state:
        emit('sync_state', state)

@socketio.on('draw')
def handle_draw(data):
    emit('draw', data, broadcast=True)
    try:
        safe_redis_command('publish', 'whiteboard_channel', json.dumps(data))
        if data.get('action') == 'clear':
            safe_redis_command('delete', STATE_KEY)
        else:
            save_state(data)
    except Exception as e:
        print(f"Redis publish error: {e}")

@socketio.on('clear_all')
def handle_clear_all():
    try:
        safe_redis_command('delete', STATE_KEY)
        clear_msg = json.dumps({'action': 'clear_all'})
        safe_redis_command('publish', 'whiteboard_channel', clear_msg)
        emit('clear_all', broadcast=True)
    except Exception as e:
        print(f"Redis clear_all error: {e}")

def redis_listener():
    if not r:
        print("Not starting redis_listener (Redis not connected).")
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

if __name__ == "__main__":
    print("Starting server...")
    if r:
        socketio.start_background_task(redis_listener)
    port = int(os.environ.get('PORT', 5001))
    socketio.run(app, host="0.0.0.0", port=port)