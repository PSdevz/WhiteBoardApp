import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template
from flask_socketio import SocketIO, emit
import redis
import json
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'a-very-secret-key-change-this'

# --- Redis Setup ---
redis_host = os.environ.get('REDIS_HOST', 'localhost')
try:
    r = redis.StrictRedis(host=redis_host, port=6379, decode_responses=True)
    r.ping()
    print("Connected to Redis successfully!")
except Exception as e:
    print(f"COULD NOT CONNECT TO REDIS at {redis_host}: {e}")
    r = None

socketio = SocketIO(app, cors_allowed_origins="*")

# --- State Snapshot ---
STATE_KEY = "whiteboard_state"

def save_state(data):
    if r:
        try:
            # Append each draw action to a list
            r.rpush(STATE_KEY, json.dumps(data))
        except Exception as e:
            print(f"Redis save_state error: {e}")

def load_state():
    if r:
        try:
            # Load the full list of draw actions
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
    # Send the full state to new clients
    state = load_state()
    if state:
        emit('sync_state', state)

@socketio.on('draw')
def handle_draw(data):
    # Broadcast locally
    emit('draw', data, broadcast=True)
    # Publish to Redis and save state
    if r:
        try:
            r.publish('whiteboard_channel', json.dumps(data))
            save_state(data)
        except Exception as e:
            print(f"Redis publish error: {e}")

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
            socketio.emit('draw', data)

# --- Start App ---
if __name__ == "__main__":
    print("Starting server...")
    if r:
        socketio.start_background_task(redis_listener)
    port = int(os.environ.get('PORT', 5001))
    socketio.run(app, host="0.0.0.0", port=port)