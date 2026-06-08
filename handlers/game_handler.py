"""
Socket event handlers for game-related operations.
"""

import time
import random
import uuid
import threading
from flask import request
from flask_socketio import emit
from utils.helpers import get_question_pair


class GameHandler:
    """Handles game-related socket events."""
    
    def __init__(self, db_manager, game_manager, socketio):
        self.db_manager = db_manager
        self.game_manager = game_manager
        self.socketio = socketio
    
    def handle_start_game(self, data):
        """Handle game start request."""
        room_id = data.get("roomId")
        player_id = data.get("playerId")
        settings = data.get("settings")

        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room: 
                return
            
            room["settings"] = settings 

            if room["host_id"] != player_id:
                emit('error_event', {'message': 'Only the host can start the game.'}, room=request.sid)
                return
            if len(room["players"]) < 2:
                emit('error_event', {'message': 'You need at least 2 players to start.'}, room=request.sid)
                return
            
            # 🆕 Get active players only
            from utils.helpers import get_active_players
            active_players = get_active_players(room["players"])
            
            print(f"🎮 GAME START DEBUG:")
            print(f"   Total players in room: {len(room['players'])}")
            print(f"   Active players: {len(active_players)} - {[p['name'] for p in active_players]}")
            print(f"   Game mode: {settings.get('gameMode', 'normal')}")
            
            room_language = room.get("language", "en")
            q_pair = get_question_pair(used_indexes=room.get("used_question_indexes", []), language=room_language)
            room["main_question"] = q_pair[0]

            if "used_question_indexes" not in room:
                room["used_question_indexes"] = []
            room["used_question_indexes"].append(q_pair[2])

            total_rounds = settings.get("totalRounds", 5) if settings else 5
            room['current_round'] = 1
            room['total_rounds'] = total_rounds

            game_mode = room.get("settings", {}).get("gameMode", "normal")
            if game_mode == "mayhem":
                impostor_count = self.game_manager.get_mayhem_impostor_count(len(active_players))
            else:
                impostor_count = 1

            print(f"   Impostor count: {impostor_count}")

            # 🆕 Select impostors from ACTIVE players
            impostors = random.sample(active_players, impostor_count) if impostor_count > 0 else []
            impostor_ids = [imp["id"] for imp in impostors]

            print(f"   Selected impostors: {[imp['name'] for imp in impostors]}")

            room["impostor_ids"] = impostor_ids
            room["imposter_id"] = impostor_ids[0] if impostor_ids else None

            # Assign roles to ALL players (including any disconnected)
            for p in room["players"]:
                is_imposter = p["id"] in impostor_ids
                room["roles"][p["id"]] = "imposter" if is_imposter else "normal"
                room["questions"][p["id"]] = q_pair[1] if is_imposter else q_pair[0]
                print(f"   {p['name']}: {room['roles'][p['id']]} - Q: {room['questions'][p['id']][:50]}...")

            room["answers"], room["votes"], room["results"] = {}, {}, {}
            
            self.socketio.emit('game_starting', room=room_id)
            
            room["phase"] = "question"
            room["questionPhaseStartTimestamp"] = int(time.time() * 1000)
            answer_time_seconds = room.get("settings", {}).get("answerTime", 60)
            room["questionPhaseEndTimestamp"] = int(time.time() * 1000) + (answer_time_seconds * 1000)
            room["lobby_events"].append("The game has started!")
            
            self.db_manager.update_room(room_id, room)

        self.game_manager.schedule_phase_transition(room_id, 'question', answer_time_seconds, 'voting')

        self.game_manager.emit_state_update(room_id, room)
    
    def handle_submit_answer(self, data):
        """Handle answer submission."""
        room_id = data.get("roomId")
        player_id = data.get("playerId")
        answer = data.get("answer")

        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room or room["phase"] != "question":
                return

            # ✅ CRITICAL: Only allow submissions during question phase
            if room["phase"] != "question":
                print(f"⚠️ Cannot submit answer - phase is {room['phase']}, not 'question'")
                return

            is_new_submission = player_id not in room["answers"]
            room["answers"][player_id] = answer

            player_name = next((p["name"] for p in room["players"] if p["id"] == player_id), "Someone")

            if is_new_submission:
                room["lobby_events"].append(f"{player_name} submitted their answer.")
            else:
                room["lobby_events"].append(f"{player_name} updated their answer.")

            # Check if all ACTIVE players have answered
            from utils.helpers import get_active_players
            active_players = get_active_players(room["players"])
            
            # Get list of active player IDs for comparison
            active_player_ids = [p["id"] for p in active_players]
            # Count how many active players have submitted
            active_submitted = [pid for pid in room["answers"].keys() if pid in active_player_ids]
            
            print(f"🔍 SUBMIT CHECK - Total players: {len(room['players'])}, Active: {len(active_players)}")
            print(f"   Active player IDs: {active_player_ids}")
            print(f"   All answers from: {list(room['answers'].keys())}")
            print(f"   Active players who submitted: {active_submitted}")
            print(f"   Count: {len(active_submitted)} / {len(active_players)}")
            
            if len(active_submitted) == len(active_players):
                print(f"   ✅ All active players submitted - transitioning to voting")
                room["phase"] = "voting"
                room["votingPhaseStartTimestamp"] = int(time.time() * 1000)
                discuss_time_seconds = room.get("settings", {}).get("discussTime", 180)
                room["votingPhaseEndTimestamp"] = int(time.time() * 1000) + (discuss_time_seconds * 1000)
                room["lobby_events"].append("All answers are in! Time to vote.")
                room['ready_to_vote'] = [] 
                
            # Update room in database
            self.db_manager.update_room(room_id, room)
        
        # Outside lock: schedule if we transitioned
        if room.get('phase') == 'voting':
            discuss_time_seconds = room.get("settings", {}).get("discussTime", 180)
            self.game_manager.schedule_phase_transition(room_id, 'voting', discuss_time_seconds, 'vote_selection')
        
        self.game_manager.emit_state_update(room_id, room)
    
    def handle_remove_answer(self, data):
        """Handle answer removal (editing)."""
        room_id = data.get("roomId")
        player_id = data.get("playerId")

        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room or room["phase"] != "question": 
                return

            if room["phase"] != "question":
                print(f"⚠️ Cannot remove answer - phase is {room['phase']}, not 'question'")
                emit('error_event', {
                    'message': 'Cannot edit answer after question phase has ended.'
                }, room=request.sid)
                return

            # Remove the player's answer
            if player_id in room["answers"]:
                del room["answers"][player_id]
                
                player_name = next((p["name"] for p in room["players"] if p["id"] == player_id), "Someone")
                room["lobby_events"].append(f"{player_name} is editing their answer.")
                
                # Update room in database
                self.db_manager.update_room(room_id, room)

        self.game_manager.emit_state_update(room_id)
    
    def handle_submit_vote(self, data):
        """Handle vote submission."""
        room_id = data.get("roomId")
        voter_id = data.get("playerId")
        voted_for_id = data.get("votedForId")

        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room or room["phase"] != "voting": 
                return
            if voter_id in room["votes"]: 
                return  # Prevent re-submission

            room["votes"][voter_id] = voted_for_id
            voter_name = next((p["name"] for p in room["players"] if p["id"] == voter_id), "Someone")
            room["lobby_events"].append(f"{voter_name} has cast their vote.")
            
            # Update room in database
            self.db_manager.update_room(room_id, room)
        
        self.game_manager.emit_state_update(room_id, room)
    
    def handle_ready_to_vote(self, data):
        """Handle ready to vote signal."""
        room_id = data.get('roomId')
        player_id = data.get('playerId')

        if not room_id or not player_id:
            return

        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room:
                return

            # Ensure the list exists
            if 'ready_to_vote' not in room:
                room['ready_to_vote'] = []

            # Add player if not already there
            if player_id not in room['ready_to_vote']:
                room['ready_to_vote'].append(player_id)
                player_name = next((p["name"] for p in room["players"] if p["id"] == player_id), "Someone")
                room["lobby_events"].append(f"{player_name} is ready to vote.")
                
                self.db_manager.update_room(room_id, room)

        self.game_manager.emit_state_update(room_id)
        
        # Check if we need to transition - USE ACTIVE PLAYERS
        from utils.helpers import get_active_players
        room = self.db_manager.get_room(room_id)
        if room:
            active_players = get_active_players(room['players'])
            ready_count = len(room.get('ready_to_vote', []))
            active_count = len(active_players)
            
            print(f"🔍 READY CHECK - Ready: {ready_count}, Active: {active_count}, Active players: {[p['name'] for p in active_players]}")
            
            # Only transition from voting phase
            if room['phase'] == 'voting' and ready_count == active_count:
                self.game_manager.transition_to_vote_selection(room_id)
    
    def handle_liar_vote(self, data):
        """Handle liar vote submission."""
        room_id = data.get('roomId')
        voter_id = data.get('playerId')
        target_id = data.get('targetId')

        if not room_id or not voter_id or not target_id:
            return

        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room or room['phase'] != 'vote_selection':
                return

            if 'liarVotes' not in room:
                room['liarVotes'] = {}

            # Get game mode to determine voting behavior
            game_mode = room.get("settings", {}).get("gameMode", "normal")

            if game_mode != "mayhem":
                # Normal mode: Remove previous vote (only one vote allowed)
                for voters in room['liarVotes'].values():
                    if voter_id in voters:
                        voters.remove(voter_id)
            # In mayhem mode: Allow multiple votes, don't remove previous ones

            if target_id not in room['liarVotes']:
                room['liarVotes'][target_id] = []

            # Add the vote (even if duplicate in mayhem mode)
            room['liarVotes'][target_id].append(voter_id)

            voter_name = next((p["name"] for p in room["players"] if p["id"] == voter_id), "Someone")
            target_name = next((p["name"] for p in room["players"] if p["id"] == target_id), "Unknown")
            room["lobby_events"].append(f"{voter_name} voted for {target_name}.")
            
            # Update room in database
            self.db_manager.update_room(room_id, room)

        self.game_manager.emit_state_update(room_id)
    
    def handle_voting_timer_expired(self, data):
        """Handle voting phase timer expiration."""
        room_id = data['roomId']
        print(f"🟡 Event received: voting_timer_expired for room {room_id}")

        if self.db_manager.room_exists(room_id):
            print(f"✅ Timer expired in voting phase — transitioning room {room_id}")
            with self.game_manager.lock:
                room = self.db_manager.get_room(room_id)
                if room:
                    room['phase'] = 'vote_selection'
                    room['voteSelectionStartTimestamp'] = int(time.time() * 1000)
                    room['voteSelectionEndTimestamp'] = int(time.time() * 1000) + 30000
                    self.db_manager.update_room(room_id, room)

            self.game_manager.emit_state_update(room_id)
    
    def handle_update_settings(self, data):
        """Handle game settings update."""
        room_id = data.get("roomId")
        new_settings = data.get("settings")

        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room:
                return

            room["settings"] = new_settings
            room["lobby_events"].append("Host updated the game settings.")
            
            # Update room in database
            self.db_manager.update_room(room_id, room)

        self.game_manager.emit_state_update(room_id, room)

    def handle_round_transition(self, data):
        """Handle round transition request."""
        room_id = data.get("roomId")
        player_id = data.get("playerId")
        
        if not room_id or not player_id:
            return
        
        print(f"Round transition request from player {player_id} in room {room_id}")
        self.game_manager.handle_round_transition(room_id)

    def handle_new_game(self, data):
        """Handle new game request from host."""
        room_id = data.get("roomId")
        player_id = data.get("playerId")
        
        print(f"🔄 NEW GAME request from player {player_id} in room {room_id}")
        
        with self.game_manager.lock:
            room = self.db_manager.get_room(room_id)
            if not room:
                emit('error_event', {'message': 'Room not found.'}, room=request.sid)
                return
            
            # Verify requester is the host
            if room["host_id"] != player_id:
                emit('error_event', {'message': 'Only the host can start a new game.'}, room=request.sid)
                return
            
            print(f"✅ Host verified, resetting room {room_id}")
            
            # Get active players (preserve disconnected players in case they reconnect)
            from utils.helpers import get_active_players
            active_players = get_active_players(room["players"])
            
            if len(active_players) < 2:
                emit('error_event', {'message': 'You need at least 2 players to start a new game.'}, room=request.sid)
                return
            
            room_language = room.get("language", "en")
            
            # ✅ PRESERVE current player count
            current_player_count = room.get("settings", {}).get("playerCount", 6)
            
            # Reset game state while preserving players and host
            room["phase"] = "waiting"
            room["imposter_id"] = None
            room["impostor_ids"] = []
            room["roles"] = {}
            room["questions"] = {}
            room["answers"] = {}
            room["votes"] = {}
            room["results"] = {}
            room["liarVotes"] = {}
            room["ready_to_vote"] = []
            room["main_question"] = None
            room["current_round"] = 1
            room["total_rounds"] = 5
            room["used_question_indexes"] = []
            room["player_scores"] = {}
            
            # ✅ Reset settings to defaults BUT keep playerCount
            room["settings"] = {
                "totalRounds": 5,
                "playerCount": current_player_count,  # ← Keep current value
                "discussTime": 180,
                "answerTime": 60,
                "gameMode": "normal"
            }
            
            # Clear timestamps
            room.pop("questionPhaseStartTimestamp", None)
            room.pop("questionPhaseEndTimestamp", None)
            room.pop("votingPhaseStartTimestamp", None)
            room.pop("votingPhaseEndTimestamp", None)
            room.pop("voteSelectionStartTimestamp", None)
            room.pop("voteSelectionEndTimestamp", None)
            
            # Add lobby event
            room["lobby_events"].append("Host started a new game. Welcome back to the lobby!")
            
            # Update room in database
            self.db_manager.update_room(room_id, room)
            
            print(f"✅ Room {room_id} reset complete with {current_player_count} player slots")
        
        # Emit the new game state to all players
        room_state = self.game_manager.get_room_state(room_id)
        self.socketio.emit('new_game_started', {'gameState': room_state}, room=room_id)
        
        print(f"✅ new_game_started event emitted to room {room_id}")