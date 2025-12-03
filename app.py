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
# IMPORTANT: change in production
app.config['SECRET_KEY'] = 'a-very-secret-key-change-this'

# --- Redis hosts (env variables) ---
MASTER_HOST = os.environ.get('REDIS_MASTER', '192.168.64.5')
BACKUP_HOST = os.environ.get('REDIS_BACKUP', '192.168.64.16')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
STATE_KEY = 'whiteboard_state'
CHANNEL = 'whiteboard_channel'

# Helper to create a Redis client and test connectivity
def connect_redis(host):
    try:
        client = redis.StrictRedis(host=host, port=REDIS_PORT, decode_responses=True)
        client.ping()
        return client
    except Exception:
        return None

# Try initial connections
r_master = connect_redis(MASTER_HOST)
r_backup = connect_redis(BACKUP_HOST)

# active client reference
if r_master:
    print(f'Connected to MASTER Redis at {MASTER_HOST}')
    r = r_master
elif r_backup:
    print(f'Connected to BACKUP Redis at {BACKUP_HOST}')
    r = r_backup
else:
    print('❌ No Redis server available — running without Redis')
    r = None

socketio = SocketIO(app, cors_allowed_origins='*')

# -----------------------------
# State merge utilities
# -----------------------------

def list_len(client, key):
    try:
        return int(client.llen(key))
    except Exception:
        return 0


def copy_full_state(src_client, dst_client, key=STATE_KEY):
    """Copy full list state from src -> dst (replace dst contents)."""
    try:
        state = src_client.lrange(key, 0, -1)
        if not state:
            return 0
        pipe = dst_client.pipeline()
        pipe.delete(key)
        pipe.rpush(key, *state)
        pipe.execute()
        return len(state)
    except Exception as e:
        print('Error copying state:', e)
        return 0


def merge_missing(src_client, dst_client, key=STATE_KEY):
    """Append missing items from src to dst (avoid wholesale replace).
    Uses membership comparison to avoid duplicates.
    """
    try:
        src = src_client.lrange(key, 0, -1) or []
        dst = set(dst_client.lrange(key, 0, -1) or [])
        missing = [item for item in src if item not in dst]
        if missing:
            dst_client.rpush(key, *missing)
        return len(missing)
    except Exception as e:
        print('Error merging state:', e)
        return 0

# -----------------------------
# Safe command wrapper with failover and merging
# -----------------------------

def safe_redis_command(cmd, *args, **kwargs):
    global r, r_master, r_backup
    if not r:
        return None
    old_r = r
    try:
        return getattr(r, cmd)(*args, **kwargs)
    except Exception as e:
        # Try other client(s)
        print(f'Redis command {cmd} failed on {getattr(old_r, "connection_pool", None)}: {e}')
        candidates = [c for c in (r_master, r_backup) if c and c != r]
        for candidate in candidates:
            try:
                candidate.ping()
                r = candidate
                # if switching to master from backup => ensure state copy/merge
                if r == r_master and old_r == r_backup:
                    # Prefer full copy if master is empty or much smaller
                    try:
                        mlen = list_len(r_master, STATE_KEY)
                        blen = list_len(r_backup, STATE_KEY)
                        if mlen == 0 and blen > 0:
                            copied = copy_full_state(r_backup, r_master)
                            print(f'Copied {copied} items BACKUP -> MASTER')
                        elif blen > mlen:
                            merged = merge_missing(r_backup, r_master)
                            if merged:
                                print(f'Merged {merged} missing items BACKUP -> MASTER')
                    except Exception as _:
                        pass
                    # tell clients to reload from the (new) master
                    try:
                        socketio.emit('force_sync')
                    except Exception:
                        pass
                # if switching to backup from master => sync master -> backup if needed
                if r == r_backup and old_r == r_master:
                    try:
                        mlen = list_len(r_master, STATE_KEY)
                        blen = list_len(r_backup, STATE_KEY)
                        if blen == 0 and mlen > 0:
                            copied = copy_full_state(r_master, r_backup)
                            print(f'Copied {copied} items MASTER -> BACKUP')
                        elif mlen > blen:
                            merged = merge_missing(r_master, r_backup)
                            if merged:
                                print(f'Merged {merged} missing items MASTER -> BACKUP')
                    except Exception:
                        pass
                    try:
                        socketio.emit('force_sync')
                    except Exception:
                        pass

                # retry original command on new active client
                return getattr(r, cmd)(*args, **kwargs)
            except Exception:
                continue
        print('Redis command failed on all hosts.')
        return None

# -----------------------------
# Application state helpers
# -----------------------------

def save_state(data):
    safe_redis_command('rpush', STATE_KEY, json.dumps(data))


def load_state():
    items = safe_redis_command('lrange', STATE_KEY, 0, -1)
    if items:
        return [json.loads(x) for x in items]
    return []

# -----------------------------
# Flask routes / socket handlers
# -----------------------------

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
    if data.get('action') == 'clear':
        safe_redis_command('delete', STATE_KEY)
    else:
        save_state(data)
    safe_redis_command('publish', CHANNEL, json.dumps(data))

@socketio.on('clear_all')
def handle_clear_all():
    safe_redis_command('delete', STATE_KEY)
    clear_msg = json.dumps({'action': 'clear_all'})
    safe_redis_command('publish', CHANNEL, clear_msg)
    emit('clear_all', broadcast=True)

# -----------------------------
# Redis listener with proactive reconciliation
# -----------------------------

def redis_listener():
    global r, r_master, r_backup
    last_active = None
    while True:
        # ensure we have an active client
        if not r:
            r_master = connect_redis(MASTER_HOST)
            r_backup = connect_redis(BACKUP_HOST)
            r = r_master or r_backup
            if not r:
                time.sleep(2)
                continue

        # if active changed since last loop, run reconciliation
        try:
            current_active = r
            if last_active is not None and last_active != current_active:
                # we switched active client; reconcile state both ways if needed
                try:
                    # If we switched to master, ensure master has backup data
                    if current_active == r_master and r_backup:
                        mlen = list_len(r_master, STATE_KEY)
                        blen = list_len(r_backup, STATE_KEY)
                        if blen > mlen:
                            # prefer merge to avoid overwriting any master-only data
                            merged = merge_missing(r_backup, r_master)
                            if merged:
                                print(f'Reconciled: merged {merged} items BACKUP -> MASTER')
                            else:
                                # if master empty, copy full state
                                if mlen == 0:
                                    copied = copy_full_state(r_backup, r_master)
                                    print(f'Reconciled: copied {copied} items BACKUP -> MASTER')
                            socketio.emit('force_sync')
                    # If we switched to backup, ensure backup has master data
                    if current_active == r_backup and r_master:
                        blen = list_len(r_backup, STATE_KEY)
                        mlen = list_len(r_master, STATE_KEY)
                        if mlen > blen:
                            merged = merge_missing(r_master, r_backup)
                            if merged:
                                print(f'Reconciled: merged {merged} items MASTER -> BACKUP')
                            else:
                                if blen == 0:
                                    copied = copy_full_state(r_master, r_backup)
                                    print(f'Reconciled: copied {copied} items MASTER -> BACKUP')
                            socketio.emit('force_sync')
                except Exception as e:
                    print('Error during proactive reconciliation:', e)
            last_active = current_active

            # create pubsub on current client and listen
            pubsub = r.pubsub()
            pubsub.subscribe(CHANNEL)
            print('Redis listener started on', r.connection_pool.connection_kwargs.get('host'))

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
            print('Redis listener error (will retry):', e)
            # drop active client to force reconnect and reconciliation
            r = None
            time.sleep(2)

# -----------------------------
# Start server
# -----------------------------

if __name__ == '__main__':
    print('Starting server...')
    socketio.start_background_task(redis_listener)
    port = int(os.environ.get('PORT', 5001))
    socketio.run(app, host='0.0.0.0', port=port)