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
    """Attempts connection to Redis at specified host."""
    try:
        # Use decode_responses=True for easy string handling
        client = redis.StrictRedis(host=host, port=REDIS_PORT, decode_responses=True)
        client.ping()
        return client
    except Exception as e:
        # Log connection failure detail only for the initial connection attempt
        if os.environ.get('LOG_LEVEL') == 'DEBUG':
            print(f"DEBUG: Could not connect to Redis at {host}:{REDIS_PORT}. Error: {e}")
        return None

# Try connecting to master, then backup at startup
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
    """
    Executes a Redis command with automatic failover to the other host on failure.
    """
    global r, r_master, r_backup
    if not r:
        return None
    try:
        # Try current client
        return getattr(r, cmd)(*args, **kwargs)
    except Exception as current_error:
        # Current client failed. Try switching.
        print(f"Redis command failed on current active host. Error: {current_error}")

        # Order candidates so we try the inactive client first.
        candidates = [c for c in [r_master, r_backup] if c and c != r]

        for candidate in candidates:
            try:
                candidate.ping()
                # Successfully pinged the alternate host. Perform switch.
                r = candidate
                active_host_ip = r.connection_pool.connection_kwargs.get('host', 'UNKNOWN')

                # --- CRITICAL LOGGING FOR DEMONSTRATION ---
                print(f"--- 🚨 FAILLOVER SUCCESS: Switched active Redis to {active_host_ip}")

                # Retry command on the new active client
                return getattr(r, cmd)(*args, **kwargs)
            except:
                continue

        # If the command failed on all attempts
        print(f"Redis command failed on all hosts. State saving suspended.")
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
    # The publish is essential for cross-node synchronization
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
        # Check if we need to establish an initial connection or reconnect
        if not r:
            print("Waiting for Redis to become available...")
            r_master_try = connect_redis(MASTER_HOST)
            r_backup_try = connect_redis(BACKUP_HOST)
            r = r_master_try or r_backup_try
            if r:
                active_host_ip = r.connection_pool.connection_kwargs.get('host', 'UNKNOWN')
                print(f"Reconnected to Redis at {active_host_ip}")
            else:
                time.sleep(2)
                continue

        try:
            pubsub = r.pubsub()
            pubsub.subscribe('whiteboard_channel')
            print("Redis listener started...")

            # This loop runs while the Pub/Sub connection is alive
            for msg in pubsub.listen():
                if msg['type'] == 'message':
                    try:
                        data = json.loads(msg['data'])
                    except Exception:
                        continue # Ignore non-JSON messages

                    if data.get('action') == 'clear_all':
                        socketio.emit('clear_all')
                    else:
                        socketio.emit('draw', data)

        # If the Pub/Sub connection drops (e.g., Redis server goes down)
        except Exception as e:
            print(f"Redis listener error (connection dropped): {e}. Attempting full reconnect.")
            r = None
            time.sleep(2) # Wait before attempting reconnection cycle

# --- Start server ---
if __name__ == "__main__":
    print("Starting server...")
    socketio.start_background_task(redis_listener)
    port = int(os.environ.get('PORT', 5001))
    socketio.run(app, host="0.0.0.0", port=port)