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
BACKUP_HOST = os.environ.get("REDIS_BACKUP", "192.168.64.51")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))

def connect_to_redis():
    """Attempt connection to MASTER first; fallback to BACKUP if needed."""
    try:
        client = redis.StrictRedis(host=MASTER_HOST, port=REDIS_PORT, decode_responses=True)
        client.ping()
        print(f"Connected to MASTER Redis at {MASTER_HOST}")
        return client
    except Exception as e:
        print(f"MASTER Redis unavailable: {e}")
        print("Trying BACKUP Redis...")

    try:
        client = redis.StrictRedis(host=BACKUP_HOST, port=REDIS_PORT, decode_responses=True)
        client.ping()
        print(f"Connected to BACKUP Redis at {BACKUP_HOST}")
        return client
    except Exception as e:
        print(f"BACKUP Redis unavailable: {e}")

    print("❌ No Redis server available — running without Redis.")
    return None

r = connect_to_redis()

socketio = SocketIO(app, cors_allowed_origins="*")

# --- State Snapshot ---
STATE_KEY = "whiteboard_state"

def save_state(data):
    if r:
        try:
            r.rpush(STATE_KEY, json.dumps(data))
        except Exception as e:
            print(f"Redis save_state error: {e}")

def load_state():
    if r:
        try:
            return [json.loads(x) for x in r.lrange(STATE_KEY, 0, -1)]
        except Exception as e:
            print(f"Redis load_state error: {e}")
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
    # Broadcast locally
    emit('draw', data, broadcast=True)
    if r:
        try:
            r.publish('whiteboard_channel', json.dumps(data))
            # Handle clear action
            if data.get('action') == 'clear':
                r.delete(STATE_KEY)  # clear saved state
            else:
                save_state(data)
        except Exception as e:
            print(f"Redis publish error: {e}")

@socketio.on('clear_all')
def handle_clear_all():
    if r:
        try:
            r.delete(STATE_KEY)  # clear saved state
            clear_msg = json.dumps({'action': 'clear_all'})
            r.publish('whiteboard_channel', clear_msg)
            emit('clear_all', broadcast=True)
        except Exception as e:
            print(f"Redis clear_all error: {e}")

# --- Redis Listener ---
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

# --- Start App ---
if __name__ == "__main__":
    print("Starting server...")
    if r:
        socketio.start_background_task(redis_listener)
    port = int(os.environ.get('PORT', 5001))
    socketio.run(app, host="0.0.0.0", port=port)