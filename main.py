# main.py - Main entry point for Kellak Lies Game
import os
from flask import Flask
from flask_socketio import SocketIO

# Import our modularized components
from database.db_manager import DatabaseManager
from game.game_manager import GameManager
from handlers.room_handler import RoomHandler
from handlers.game_handler import GameHandler
from handlers.connection_handler import ConnectionHandler

# Initialize Flask app and SocketIO
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize components
db_manager = DatabaseManager()
game_manager = GameManager(db_manager, socketio)
room_handler = RoomHandler(db_manager, game_manager, socketio)
game_handler = GameHandler(db_manager, game_manager, socketio)
connection_handler = ConnectionHandler(db_manager, game_manager, socketio)


# Connection event handlers
@socketio.on('connect')
def handle_connect():
    connection_handler.handle_connect()


@socketio.on('disconnect')
def handle_disconnect(reason=None):
    connection_handler.handle_disconnect(reason)


@socketio.on('rejoin_game')
def handle_rejoin_game(data):
    connection_handler.handle_rejoin_game(data)


# Room management event handlers
@socketio.on('create_room')
def on_create_room(data):
    room_handler.handle_create_room(data)


@socketio.on('join_room')
def on_join_room(data):
    room_handler.handle_join_room(data)


@socketio.on('leave_room')
def on_leave_room(data):
    room_handler.handle_leave_room(data)


@socketio.on("kick_player")
def handle_kick_player(data):
    room_handler.handle_kick_player(data)


# Game event handlers
@socketio.on('start_game')
def on_start_game(data):
    game_handler.handle_start_game(data)


@socketio.on('submit_answer')
def on_submit_answer(data):
    game_handler.handle_submit_answer(data)


@socketio.on('remove_answer')
def on_remove_answer(data):
    game_handler.handle_remove_answer(data)


@socketio.on('submit_vote')
def on_submit_vote(data):
    game_handler.handle_submit_vote(data)


@socketio.on('ready_to_vote')
def handle_ready_to_vote(data):
    game_handler.handle_ready_to_vote(data)


@socketio.on('liar_vote')
def handle_liar_vote(data):
    game_handler.handle_liar_vote(data)


@socketio.on('voting_timer_expired')
def handle_voting_timer_expired(data):
    game_handler.handle_voting_timer_expired(data)


@socketio.on('update_settings')
def on_update_settings(data):
    game_handler.handle_update_settings(data)


@socketio.on('round_transition')
def on_round_transition(data):
    game_handler.handle_round_transition(data)

@socketio.on('new_game')
def on_new_game(data):
    game_handler.handle_new_game(data)

@socketio.on('request_state')
def handle_request_state(data):
    room_id = data.get('roomId')
    if room_id:
        game_manager.emit_state_update(room_id)


# HTTP route
@app.route('/', methods=['GET'])
def index():
    return "Welcome to the Kellak Lies!"


# Application entry point
if __name__ == "__main__":
    DEVELOPMENT = False
    if not DEVELOPMENT:
        port = int(os.environ.get("PORT", 5000))
        socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
    else:
        socketio.run(app, port=5000, debug=True, allow_unsafe_werkzeug=True)  # debug=True for development