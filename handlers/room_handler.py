"""
Socket event handlers for room management operations.
"""

import uuid
import time
from flask import request
from flask_socketio import emit, join_room, leave_room
from utils.helpers import validate_room_data, sanitize_string, is_name_available, get_active_players


class RoomHandler:
    """Handles room-related socket events."""
    
    def __init__(self, db_manager, game_manager, socketio):
        self.db_manager = db_manager
        self.game_manager = game_manager
        self.socketio = socketio
    
    def handle_create_room(self, data):
        """Handle room creation request."""
        room_id = sanitize_string(data.get("roomId"))
        name = sanitize_string(data.get("name"))
        user_avatar = data.get("avatar")
        language = data.get("language", "en")
        print(f"   Language received from frontend: {language}")  # ADD THIS
        
        print(f"Create room request: {request.sid} for room {room_id} with name {name} and avatar {user_avatar}")

        if not room_id or not name or not user_avatar:
            emit('error_event', {'message': 'Room ID, name, and user avatar are required.'}, room=request.sid)
            return

        with self.game_manager.lock:
            if self.db_manager.room_exists(room_id):
                emit('error_event', {'message': 'Room already exists.'}, room=request.sid)
                return

            player_id = str(uuid.uuid4())
            room_data = {
                "players": [{"id": player_id, "name": name, "avatar": user_avatar, "socket_id": request.sid}],
                "host_id": player_id,  # First player is the host
                "phase": "waiting",
                "language": language,
                "imposter_id": None,
                "roles": {},
                "questions": {},
                "answers": {},
                "votes": {},
                "results": {},
                "lobby_events": [f"{name} created the room and is the host."],
                "main_question": None,
                'ready_to_vote': [],
                'current_round': 1,
                'total_rounds': 5,
                'used_question_indexes': []
            }
            
            # Create room in database
            self.db_manager.create_room(room_id, room_data)

            join_room(room_id)
            print(f"   About to emit join_confirmation with language: {language}")
            emit('join_confirmation', {'playerId': player_id, 'roomId': room_id, 'language': language}, room=request.sid)

        self.game_manager.emit_state_update(room_id)
    
    def handle_join_room(self, data):
        """Handle room join request."""
        room_id = sanitize_string(data.get("roomId"))
        name = sanitize_string(data.get("name"))
        user_avatar = data.get("avatar")
        
        print(f"Join request: {request.sid} for room {room_id} with name {name} and avatar {user_avatar}")
        
        if not room_id or not name or not user_avatar:
            emit('error_event', {'message': 'Room ID, name, and user avatar are required.'}, room=request.sid)
            return

        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room:
                # Get default language if room doesn't exist
                requested_language = data.get("language", "en")
                emit('error_event', {'message': self.get_error_message('room_not_found', requested_language)}, room=request.sid)
                return
            
            requested_language = data.get("language", "en")
            room_language = room.get("language", "en")
            
            if requested_language != room_language:
                emit('error_event', {
                    'message': self.get_error_message('language_mismatch', requested_language),  # Use THEIR language
                    'language': requested_language  # So they get their preferred overlay
                }, room=request.sid)
                return
                            
            # Join an existing room
            if room["phase"] != "waiting":
                emit('error_event', {'message': self.get_error_message('game_in_progress', room_language)}, room=request.sid)
                return
                
            from utils.helpers import get_active_players

            max_players = room.get("settings", {}).get("playerCount", 6)
            active_players = get_active_players(room["players"])
            current_player_count = len(active_players)

            print(f"🔍 JOIN CHECK - Room {room_id}: {current_player_count} active players (out of {len(room['players'])} total), max {max_players}")
            print(f"   Active players: {[p['name'] for p in active_players]}")

            if current_player_count >= max_players:
                print(f"❌ Room full! {current_player_count} >= {max_players}")
                emit('error_event', {'message': self.get_error_message('room_full', room_language)}, room=request.sid)
                return

            if not is_name_available(room["players"], name):
                emit('error_event', {'message': self.get_error_message('name_taken', room_language)}, room=request.sid)
                return

            # ALL VALIDATION PASSED - Now add the player
            player_id = str(uuid.uuid4())
            room["players"].append({"id": player_id, "name": name, "avatar": user_avatar, "socket_id": request.sid})
            room["lobby_events"].append(f"{name} has joined the game.")
            
            self.db_manager.update_room(room_id, room)
            join_room(room_id)
            
            emit('join_confirmation', {'playerId': player_id, 'roomId': room_id, 'language': room_language}, room=request.sid)

        self.game_manager.emit_state_update(room_id)
    
    def handle_leave_room(self, data):
        """Handle room leave request."""
        room_id = data.get("roomId")
        player_id = data.get("playerId")
        
        print(f"Leave request: {request.sid} for room {room_id} with player ID {player_id}")
        
        if not room_id or not player_id:
            emit('error_event', {'message': 'Room ID and player ID are required.'}, room=request.sid)
            return

        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room:
                emit('error_event', {'message': 'The room you were trying to reach doesn\'t exist anymore.'}, room=request.sid)
                return
            
            print(f"🔍 BEFORE LEAVE - Players: {[p['name'] for p in room['players']]}")
            
            # Find the player in the room
            player_to_remove = next((p for p in room["players"] if p["id"] == player_id), None)
            if not player_to_remove:
                emit('error_event', {'message': 'Player not found in room.'}, room=request.sid)
                return
            
            # Verify the socket ID matches (security check)
            if player_to_remove.get("socket_id") != request.sid:
                emit('error_event', {'message': 'Invalid player credentials.'}, room=request.sid)
                return
            
            player_name = player_to_remove["name"]
            
            # Remove the player from the room
            room["players"] = [p for p in room["players"] if p["id"] != player_id]
            room["lobby_events"].append(f"{player_name} has left the game.")
            
            print(f"🔍 AFTER LEAVE - Players: {[p['name'] for p in room['players']]}")
            
            # Leave the socket room
            leave_room(room_id)
            
            # Send confirmation to the leaving player
            emit('leave_confirmation', {'message': 'Successfully left the room.'}, room=request.sid)
            
            # Check if room is now empty
            if not room["players"]:
                self.db_manager.delete_room(room_id)
                print(f"Room {room_id} is empty and has been removed.")
                return
            
            # If the host left, assign a new host
            if player_id == room["host_id"]:
                room["host_id"] = room["players"][0]["id"]
                new_host_name = room["players"][0]["name"]
                room["lobby_events"].append(f"{new_host_name} is the new host.")
        
            # Update room in database
            print(f"🔍 UPDATING DATABASE - Players before update: {[p['name'] for p in room['players']]}")
            self.db_manager.update_room(room_id, room)
            
            # Verify the update worked
            verify_room = self.db_manager.get_room(room_id)
            print(f"🔍 VERIFY DATABASE - Players after update: {[p['name'] for p in verify_room['players']]}")
        
        # Update all remaining players in the room
        self.game_manager.emit_state_update(room_id)
    
    def handle_kick_player(self, data):
        """Handle player kick request."""
        room_id = data.get("roomId")
        target_player_id = data.get("targetPlayerId")
        by_player_id = data.get("byPlayerId")

        print(f"KICK request: {by_player_id} is trying to kick {target_player_id} from {room_id}")

        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room:
                emit("error_event", {"message": "Room not found."}, room=request.sid)
                return

            if by_player_id != room["host_id"]:
                emit("error_event", {"message": "Only the host can kick players."}, room=request.sid)
                return

            player_to_kick = next((p for p in room["players"] if p["id"] == target_player_id), None)
            if not player_to_kick:
                emit("error_event", {"message": "Player to kick not found."}, room=request.sid)
                return

            target_socket_id = player_to_kick["socket_id"]
            player_name = player_to_kick["name"]
            
            # Get room language for translated message
            room_language = room.get("language", "en")
            
            # 1. Remove player from Socket.IO room FIRST
            leave_room(room_id, sid=target_socket_id)
            
            # 2. Update database
            room["players"] = [p for p in room["players"] if p["id"] != target_player_id]
            room["lobby_events"].append(f"{player_name} was kicked from the game.")
            self.db_manager.update_room(room_id, room)

        # 3. Send kick message (they're not in the room anymore, so won't get state update)
        print(f"🚨 EMITTING kicked_from_room to socket {target_socket_id}")
        emit('kicked_from_room', {"message": self.get_error_message('kicked', room_language)}, to=target_socket_id)
        
        # 4. Now emit state update (kicked player won't receive it)
        self.game_manager.emit_state_update(room_id)


    def get_error_message(self, key: str, room_language: str) -> str:
        """Get translated error message"""
        messages = {
            'room_exists': {
                'en': 'Room already exists.',
                'ar': '.الغرفة موجودة سابقاً'
            },
            'room_full': {
                'en': 'The room you were trying to reach seems full.',
                'ar': '.الغرفة الي كنت تحاول توصلها ممتلئة'
            },
            'name_taken': {
                'en': 'That name is already taken.',
                'ar': '.هذا الاسم مستخدم'
            },
            'room_not_found': {
                'en': "The room you were trying to reach doesn't exist anymore.",
                'ar': '.الغرفة الي كنت تحاول توصلها مهي موجودة'
            },
            'game_in_progress': {
                'en': 'Game is already in progress.',
                'ar': '.مايمديك تدخل لعبة جارية '
            },
            'language_mismatch': {
                'en': "Room language mismatch.",
                'ar': ".عدم تطابق لغة الغرفة"
            },
            'kicked': {
                'en': 'You have been removed from the game.',
                'ar': '.تم طردك من اللعبة'
            },

            'solo_player_kick': {  # ADD THIS
                'en': 'You were the only player left in the game.',
                'ar': '.كنت آخر لاعب في اللعبة'
        }
        }
        return messages.get(key, {}).get(room_language, messages[key]['en'])

    def is_player_in_another_room(self, player_socket_id, target_room_id=None):
        """Check if this socket is already in a different room."""
        # Check all rooms
        for room_id in self.db_manager.get_all_room_ids():
            # Skip the room they're trying to join (for rejoin scenarios)
            if room_id == target_room_id:
                continue
                
            room = self.db_manager.get_room(room_id)
            if not room:
                continue
            
            # Check if this socket_id is in the room
            for player in room["players"]:
                if player.get("socket_id") == player_socket_id and not player.get("disconnected"):
                    return room_id  # Found them in another room
        
        return None