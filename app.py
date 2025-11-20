from flask import Flask, render_template
from flask_socketio import SocketIO, emit
import redis
import json
import eventlet # Makes the server high-performance
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'a-very-secret-key-that-you-should-change'

# --- Redis Setup ---
# Kubernetes will find the service named 'redis-server'
redis_host = os.environ.get('REDIS_HOST', 'localhost')
try:
    r = redis.StrictRedis(host=redis_host, port=6379, decode_responses=True)
    r.ping() # Test the connection
    print("Connected to Redis successfully!")
except Exception as e:
    print(f"COULD NOT CONNECT TO REDIS at {redis_host}: {e}")
    r = None # Set to None if connection fails

socketio = SocketIO(app, cors_allowed_origins="*")

# --- Main App Routes ---

@app.route('/')
def index():
    """Serves the main HTML page."""
    # This looks for index.html in a folder named 'templates'
    return render_template('index.html')

# --- SocketIO Handlers for Drawing ---

@socketio.on('draw')
def handle_draw(data):
    """
    Called when a client sends a 'draw' message.
    It broadcasts to local clients AND publishes to Redis.
    """
    # 1. Broadcast to all clients connected to THIS instance
    emit('draw', data, broadcast=True)

    # 2. Publish to Redis for all OTHER instances
    if r:
        try:
            r.publish('whiteboard_channel', json.dumps(data))
        except Exception as e:
            print(f"Redis publish error: {e}")

# --- Redis Listener (Runs in background) ---

def redis_listener():
    """
    Listens to the Redis 'whiteboard_channel'.
    When a message comes from another instance, it
    broadcasts it to clients on THIS instance.
    """
    if not r:
        print("Not starting redis_listener (Redis not connected).")
        return # Do nothing if Redis isn't connected

    pubsub = r.pubsub()
    pubsub.subscribe('whiteboard_channel')
    print("Redis listener started...")
    for msg in pubsub.listen():
        if msg['type'] == 'message':
            data = json.loads(msg['data'])
            # Emit to this instance's clients
            socketio.emit('draw', data)

# --- Start the App ---

if __name__ == "__main__":
    print("Starting server...")
    # Start the background task
    if r:
        socketio.start_background_task(redis_listener)

    # Get port from environment or default to 5001
    port = int(os.environ.get('PORT', 5001))

    # Run the app.
    # allow_unsafe_werkzeug=True is for local testing.
    # In a real K8s deployment, Gunicorn would be used, but this is fine.
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)