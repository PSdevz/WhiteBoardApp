from flask import Flask, render_template
from flask_socketio import SocketIO, emit
import redis
import json
import os
import time
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'a-very-secret-key-change-this'

# --- Redis Setup ---
MASTER_HOST = os.environ.get("REDIS_MASTER", "192.168.64.5")
BACKUP_HOST = os.environ.get("REDIS_BACKUP", "192.168.64.16")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
STATE_KEY = "whiteboard_state"

# Use threading backend instead of eventlet
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Global redis objects
r_master = None
r_backup = None
r = None  # active redis client


def connect_redis(host):
    """Try connecting to Redis."""
    try:
        client = redis.StrictRedis(host=host, port=REDIS_PORT, decode_responses=True)
        client.ping()
        return client
    except:
        return None


# Initial connections
r_master = connect_redis(MASTER_HOST)
r_backup = connect_redis(BACKUP_HOST)

if r_master:
    print(f"Connected to MASTER Redis at {MASTER_HOST}")
    r = r_master
elif r_backup:
    print(f"Connected to BACKUP Redis at {BACKUP_HOST}")
    r = r_backup
else:
    print("❌ No Redis server available — running WITHOUT Redis")
    r = None


# -------------------------------------------------------------------
#   FULL STATE SYNC HELPERS
# -------------------------------------------------------------------
def copy_state(source, target):
    """Copy all whiteboard state from source Redis to target Redis."""
    try:
        state = source.lrange(STATE_KEY, 0, -1) or []
        pipe = target.pipeline()
        pipe.delete(STATE_KEY)
        if state:
            pipe.rpush(STATE_KEY, *state)
        pipe.execute()
        return True
    except Exception as e:
        print("State copy error:", e)
        return False


# -------------------------------------------------------------------
#   SAFE REDIS COMMAND WITH AUTO-FAILOVER
# -------------------------------------------------------------------
def safe_redis_command(cmd, *args, **kwargs):
    global r, r_master, r_backup
    if not r:
        return None

    try:
        return getattr(r, cmd)(*args, **kwargs)

    except Exception as err:
        print("Redis command failed:", err)

        candidates = [c for c in [r_master, r_backup] if c and c != r]

        for cand in candidates:
            try:
                cand.ping()
                old_r = r
                r = cand

                # Perform full sync depending on failover direction
                if r == r_master and r_backup:
                    print("### FULL SYNC BACKUP → MASTER ###")
                    copy_state(r_backup, r_master)

                elif r == r_backup and r_master:
                    print("### FULL SYNC MASTER → BACKUP ###")
                    copy_state(r_master, r_backup)

                print("Failover → now using:", cand)
                return getattr(r, cmd)(*args, **kwargs)

            except:
                continue

        print("Both Redis hosts unreachable.")
        return None


# -------------------------------------------------------------------
#   STATE HELPERS
# -------------------------------------------------------------------
def save_state(data):
    safe_redis_command('rpush', STATE_KEY, json.dumps(data))


def load_state():
    items = safe_redis_command('lrange', STATE_KEY, 0, -1)
    if items:
        return [json.loads(x) for x in items]
    return []


# -------------------------------------------------------------------
#   ROUTES
# -------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


# -------------------------------------------------------------------
#   SOCKET HANDLERS
# -------------------------------------------------------------------
@socketio.on('connect')
def on_connect():
    state = load_state()
    if state:
        emit('sync_state', state)


@socketio.on('draw')
def on_draw(data):
    emit('draw', data, broadcast=True)

    if data.get('action') == 'clear':
        safe_redis_command('delete', STATE_KEY)
    else:
        save_state(data)

    safe_redis_command('publish', 'whiteboard_channel', json.dumps(data))


@socketio.on('clear_all')
def on_clear_all():
    safe_redis_command('delete', STATE_KEY)
    safe_redis_command('publish', 'whiteboard_channel', json.dumps({'action': 'clear_all'}))
    emit('clear_all', broadcast=True)


# ---------------------------------------------------------------
#   REDIS LISTENER THREAD
# ---------------------------------------------------------------
def redis_listener():
    global r, r_master, r_backup

    while True:
        try:
            if not r:
                # Try reconnecting
                r_master_try = connect_redis(MASTER_HOST)
                r_backup_try = connect_redis(BACKUP_HOST)
                new_r = r_master_try or r_backup_try

                if new_r:
                    print("Reconnected to Redis:", new_r)
                    r_master = r_master_try
                    r_backup = r_backup_try
                    r = new_r

                    # full sync on reconnect
                    if r == r_master and r_backup:
                        copy_state(r_backup, r_master)
                    elif r == r_backup and r_master:
                        copy_state(r_master, r_backup)

                    socketio.emit('force_sync')

                time.sleep(1)
                continue

            # Subscribe
            pubsub = r.pubsub()
            pubsub.subscribe('whiteboard_channel')

            print("Redis listener running...")

            for msg in pubsub.listen():
                if msg['type'] != 'message':
                    continue

                data = json.loads(msg['data'])

                if data.get('action') == 'clear_all':
                    socketio.emit('clear_all')
                else:
                    socketio.emit('draw', data)

        except Exception as e:
            print("Redis listener error:", e)
            r = None
            time.sleep(1)


# Start listener thread
threading.Thread(target=redis_listener, daemon=True).start()


# -------------------------------------------------------------------
#   START SERVER (THREADING MODE)
# -------------------------------------------------------------------
if __name__ == "__main__":
    print("Starting server WITHOUT eventlet...")
    socketio.run(app, host="0.0.0.0", port=5001, use_reloader=False)