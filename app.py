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
# IMPORTANT: Change this in a production environment
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

# ----------------------------------------------------------------------
# --- Safe Redis commands with automatic failover AND STATE SYNC ---
# ----------------------------------------------------------------------
def safe_redis_command(cmd, *args, **kwargs):
    """
    Executes a Redis command with automatic failover and state synchronization.
    """
    global r, r_master, r_backup
    if not r:
        return None

    # Store the client *before* the retry attempt for comparison
    old_r = r

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

                # --- FAILLOVER/FAILBACK DETECTED ---
                r = candidate
                active_host_ip = r.connection_pool.connection_kwargs.get('host', 'UNKNOWN')

                # CRITICAL FIX: If switching *to* the Master (failback) and the Master is empty, sync from the old (Backup)
                if r == r_master and old_r == r_backup:
                    # Check if the Master is empty but the old active Backup has data
                    if not r.exists(STATE_KEY) and old_r.exists(STATE_KEY):
                        # Use a pipeline for atomic copy of all list items
                        pipe = old_r.pipeline()
                        pipe.lrange(STATE_KEY, 0, -1)
                        # Execute the pipeline to get the state from the old server
                        state_to_copy = pipe.execute()[0]

                        if state_to_copy:
                            # Use another pipeline to set the state on the new Master
                            r_pipe = r.pipeline()
                            r_pipe.delete(STATE_KEY) # Ensure clean start
                            r_pipe.rpush(STATE_KEY, *state_to_copy)
                            r_pipe.execute()
                            print("--- ✅ STATE SYNC SUCCESS: Copied state from Backup to newly active Master.")

                # --- CRITICAL LOGGING FOR DEMONSTRATION ---
                print(f"--- 🚨 FAILLOVER SUCCESS: Switched active Redis to {active_host_ip}")

                # Retry command on the new active client
                return getattr(r, cmd)(*args, **kwargs)
            except:
                continue

        # If the command failed on all attempts
        print(f"Redis command failed on all hosts. State saving suspended.")
        return None

# ----------------------------------------------------------------------
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
    # You must have an 'index.html' file in a 'templates' folder
    return render_template('index.html')

# --- SocketIO Handlers ---
@socketio.on('connect')
def handle_connect():
    # A client connects, send them the current state
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
    # The publish is essential for cross-node synchronization (e.g., if you run multiple Flask instances)
    safe_redis_command('publish', 'whiteboard_channel', json.dumps(data))

@socketio.on('clear_all')
def handle_clear_all():
    safe_redis_command('delete', STATE_KEY)
    clear_msg = json.dumps({'action': 'clear_all'})
    safe_redis_command('publish', 'whiteboard_channel', clear_msg)
    emit('clear_all', broadcast=True)

# ----------------------------------------------------------------------
# --- Redis Listener (always active, auto-reconnect, force sync on client) ---
# ----------------------------------------------------------------------
def redis_listener():
    global r, r_master, r_backup
    while True:
        # Check if we need to establish an initial connection or reconnect
        if not r:
            print("Waiting for Redis to become available...")

            # Save current state of r
            r_was_none = (r is None)

            # Re-attempt connections
            r_master = connect_redis(MASTER_HOST)
            r_backup = connect_redis(BACKUP_HOST)

            r = r_master or r_backup

            if r:
                active_host_ip = r.connection_pool.connection_kwargs.get('host', 'UNKNOWN')
                print(f"Reconnected to Redis at {active_host_ip}")

                # Don't force a client sync just because Redis reconnected.
                # We will only force a sync after an actual state reconciliation (copy/merge)
                # which is handled in the failover code paths where we modify the state.

            else:
                time.sleep(2)
                continue

        try:
            # Note: We must create a *new* pubsub object on the current active 'r' client
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

                    # Fan out the Pub/Sub message to all connected SocketIO clients
                    if data.get('action') == 'clear_all':
                        # Use skip_sid=request.sid for the original draw handler, but here we must emit to all
                        socketio.emit('clear_all', broadcast=True)
                    else:
                        socketio.emit('draw', data, broadcast=True)

        # If the Pub/Sub connection drops (e.g., Redis server goes down)
        except Exception as e:
            print(f"Redis listener error (connection dropped): {e}. Attempting full reconnect.")
            r = None
            time.sleep(2) # Wait before attempting reconnection cycle

# --- Start server ---
if __name__ == "__main__":
    print("Starting server...")
    # Start the Redis listener in a separate thread
    socketio.start_background_task(redis_listener)
    port = int(os.environ.get('PORT', 5001))
    # run the server
    socketio.run(app, host="0.0.0.0", port=port)