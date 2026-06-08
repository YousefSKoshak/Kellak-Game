"""
Socket event handlers for connection management operations.
"""

import time
from flask import request
from flask_socketio import emit, join_room
from utils.helpers import get_active_players


class ConnectionHandler:
    """Handles connection-related socket events."""
    
    def __init__(self, db_manager, game_manager, socketio):
        self.db_manager = db_manager
        self.game_manager = game_manager
        self.socketio = socketio
    
    def handle_connect(self):
        """Handle client connection."""
        print(f"Client connected: {request.sid}")
    
    def handle_disconnect(self, reason=None):
        """Handle client disconnection."""
        print(f"Client disconnected: {request.sid} (reason: {reason})")
        
        with self.game_manager.lock:
            room_to_update = None
            for room_id in self.db_manager.get_all_room_ids():
                room = self.db_manager.get_room(room_id)
                if not room:
                    continue
                    
                player_to_update = next((p for p in room["players"] if p.get("socket_id") == request.sid), None)

                if player_to_update:
                    player_id = player_to_update["id"]
                    player_name = player_to_update["name"]
                    
                    # If in waiting phase, remove completely
                    if room["phase"] == "waiting":
                        room["players"] = [p for p in room["players"] if p["id"] != player_id]
                        room["lobby_events"].append(f"{player_name} has left the game.")
                        
                        if not room["players"]:
                            self.db_manager.delete_room(room_id)
                            print(f"Room {room_id} is empty and has been removed.")
                            return
                        
                        if player_id == room["host_id"] and room["players"]:
                            room["host_id"] = room["players"][0]["id"]
                            new_host_name = room["players"][0]["name"]
                            room["lobby_events"].append(f"{new_host_name} is the new host.")
                        
                        self.db_manager.update_room(room_id, room)
                        room_to_update = room_id
                        break
                    
                    # For active game: Mark as disconnected
                    print(f"Player {player_name} disconnected during {room['phase']} phase")
                    player_to_update["disconnected"] = True
                    player_to_update["disconnect_time"] = time.time()
                    player_to_update.pop("socket_id", None)
                    
                    # Save submission state
                    player_to_update["had_submitted"] = player_id in room.get("answers", {})
                    player_to_update["was_ready"] = player_id in room.get("ready_to_vote", [])
                    
                    room["lobby_events"].append(f"{player_name} has disconnected.")
                    
                    # Remove game data based on phase
                    # Only remove answers if in question phase (answers aren't public yet)
                    if room["phase"] == "question" and player_id in room.get("answers", {}):
                        del room["answers"][player_id]
                        print(f"   Removed answer (question phase)")
                    
                    # Always remove votes and ready status (can be resubmitted)
                    if player_id in room.get("votes", {}):
                        del room["votes"][player_id]
                    
                    if player_id in room.get("ready_to_vote", []):
                        room["ready_to_vote"].remove(player_id)
                        print(f"   Removed from ready_to_vote")
                    
                    # Remove from liar votes
                    if "liarVotes" in room:
                        for target_id, voters in list(room["liarVotes"].items()):
                            if player_id in voters:
                                voters.remove(player_id)
                        if player_id in room["liarVotes"]:
                            del room["liarVotes"][player_id]
                    
                    # Clean up expired players
                    current_time = time.time()
                    expired_players = [p for p in room["players"] 
                                    if p.get("disconnected") and (current_time - p.get("disconnect_time", 0)) > 30]
                    
                    for expired_player in expired_players:
                        room["players"] = [p for p in room["players"] if p["id"] != expired_player["id"]]
                        room["lobby_events"].append(f"{expired_player['name']} has been removed (reconnect timeout).")
                        print(f"   Removed expired player: {expired_player['name']}")
                    
                    from utils.helpers import get_active_players
                    active_players = get_active_players(room["players"])
                    
                    print(f"   Active players remaining: {len(active_players)} - {[p['name'] for p in active_players]}")

                    if len(active_players) == 1:
                        last_player = active_players[0]
                        last_player_sid = last_player.get("socket_id")
                        if last_player_sid:
                            room_language = room.get('language', 'en')
                            solo_kick_message = {
                                'en': 'You were the only player left in the game.',
                                'ar': '.كنت آخر لاعب في اللعبة'
                            }.get(room_language, 'You were the only player left in the game.')

                            self.socketio.emit('solo_player_kick', {
                                'message': solo_kick_message
                            }, room=last_player_sid)
                        
                        self.db_manager.delete_room(room_id)
                        print(f"Room {room_id} had only 1 active player and was deleted.")
                        return
                    
                    if not active_players:
                        self.db_manager.delete_room(room_id)
                        print(f"Room {room_id} has no active players and has been removed.")
                        return

                    if player_id == room["host_id"] and active_players:
                        room["host_id"] = active_players[0]["id"]
                        new_host_name = active_players[0]["name"]
                        room["lobby_events"].append(f"{new_host_name} is the new host.")

                    self.check_phase_transition_after_disconnect(room, room_id, active_players)
                    
                    # ✅ Extract scheduled transitions before saving
                    schedule_voting = room.pop("_schedule_voting_transition", None)
                    schedule_vote_selection = room.pop("_schedule_vote_selection_transition", None)

                    self.db_manager.update_room(room_id, room)
                    room_to_update = room_id
                    
                    # ✅ Schedule timers OUTSIDE the lock
                    if schedule_voting:
                        self.game_manager.schedule_phase_transition(room_id, 'voting', schedule_voting, 'vote_selection')
                    if schedule_vote_selection:
                        self.game_manager.schedule_phase_transition(room_id, 'vote_selection', 30, 'results')
                    
                    break
        
        if room_to_update:
            self.game_manager.emit_state_update(room_to_update)

    def check_phase_transition_after_disconnect(self, room, room_id, active_players):
        """Check if phase should transition after a player disconnects."""
        phase = room["phase"]
        
        if phase == "question":
            answers_count = len(room.get("answers", {}))
            active_count = len(active_players)
            submitted_ids = list(room.get("answers", {}).keys())
            active_ids = [p["id"] for p in active_players]
            
            print(f"   🔍 PHASE TRANSITION CHECK (question):")
            print(f"      Active players: {active_count} - {[p['name'] for p in active_players]}")
            print(f"      Active IDs: {active_ids}")
            print(f"      Submitted count: {answers_count}")
            print(f"      Submitted IDs: {submitted_ids}")
            print(f"      All active submitted? {answers_count == active_count and active_count > 0}")
            
            if answers_count == len(active_players) and len(active_players) > 0:
                print(f"   ✅ All active players answered, transitioning to voting")
                import time
                room["phase"] = "voting"
                room["votingPhaseStartTimestamp"] = int(time.time() * 1000)
                discuss_time = room.get("settings", {}).get("discussTime", 180)
                room["votingPhaseEndTimestamp"] = int(time.time() * 1000) + (discuss_time * 1000)
                room["lobby_events"].append("All answers are in! Time to vote.")
                room['ready_to_vote'] = []
                room["_schedule_voting_transition"] = discuss_time  # ✅ signal to caller
        
        elif phase == "voting":
            ready_count = len(room.get("ready_to_vote", []))
            active_count = len(active_players)
            
            print(f"   🔍 PHASE TRANSITION CHECK (voting):")
            print(f"      Ready count: {ready_count}")
            print(f"      Active count: {active_count}")
            print(f"      All active ready? {ready_count == active_count and active_count > 0}")
            
            if ready_count == len(active_players) and len(active_players) > 0:
                print(f"   ✅ All active players ready, transitioning to vote_selection")
                import time
                room['phase'] = 'vote_selection'
                room['voteSelectionStartTimestamp'] = int(time.time() * 1000)
                room['voteSelectionEndTimestamp'] = int(time.time() * 1000) + 30000
                room["lobby_events"].append("Time to vote for the imposter!")
                room["liarVotes"] = {}
                room['ready_to_vote'] = []
                room["_schedule_vote_selection_transition"] = True  # ✅ signal to caller
    
    def handle_rejoin_game(self, data):
        """Handle player rejoin request."""
        room_id = data.get("roomId")
        player_id = data.get("playerId")
        timestamp = data.get("timeStamp")

        print(f"🔄 Rejoin request: SID={request.sid}, Room={room_id}, Player={player_id}")

        if not room_id or not player_id:
            emit('reconnect_player', {'success': False, 'message': 'Room ID and Player ID are required.'})
            return

        try:
            with self.game_manager.lock:
                room = self.db_manager.get_room(room_id)
                if not room:
                    print(f"❌ Room {room_id} does not exist")
                    emit('reconnect_player', {'success': False, 'message': 'Room does not exist.'})
                    return
                
                requested_language = data.get("language", "en")
                room_language = room.get("language", "en")
                if requested_language != room_language:
                    emit('reconnect_player', {'success': False, 'message': "Room doesn't exist."})
                    return

                player_to_rejoin = next((p for p in room["players"] if p["id"] == player_id), None)
                
                if not player_to_rejoin:
                    print(f"❌ Player {player_id} not found in room {room_id}")
                    emit('reconnect_player', {'success': False, 'message': 'Player not found in room.'})
                    return

                if not player_to_rejoin.get("disconnected"):
                    print(f"❌ Player {player_id} is not marked as disconnected")
                    emit('reconnect_player', {'success': False, 'message': 'Player is not disconnected.'})
                    return

                disconnect_time = player_to_rejoin.get("disconnect_time", 0)
                elapsed = time.time() - disconnect_time
                if elapsed > 30:
                    print(f"❌ Reconnection time window expired ({elapsed:.1f}s)")
                    emit('reconnect_player', {'success': False, 'message': 'Reconnection time window has expired.'})
                    return

                print(f"✅ Player rejoining (disconnected for {elapsed:.1f}s)")

                # Restore connection
                player_to_rejoin["disconnected"] = False
                player_to_rejoin.pop("disconnect_time", None)
                player_to_rejoin["socket_id"] = request.sid
                
                # 🔧 FIX: Check if they had already submitted/voted BEFORE disconnecting
                had_submitted = player_to_rejoin.pop("had_submitted", False)
                was_ready = player_to_rejoin.pop("was_ready", False)
                
                print(f"   Restoring state: had_submitted={had_submitted}, was_ready={was_ready}")
                
                join_room(room_id)
                
                room["lobby_events"].append(f"{player_to_rejoin['name']} has reconnected.")

                self.db_manager.update_room(room_id, room)

            # Send updated state
            room_state = self.game_manager.get_room_state(room_id)
            
            # Send personal info if in question phase
            if room_state["phase"] == "question":
                personal_info = {
                    "role": room["roles"].get(player_id),
                    "question": room["questions"].get(player_id)
                }
                emit('personal_game_info', personal_info, room=request.sid)

            # 🔧 FIX: Tell frontend their exact submission state
            emit('reconnect_player', {
                'success': True,
                'message': 'Successfully reconnected to the game',
                'gameState': room_state,
                'playerId': player_id,
                'hadSubmitted': had_submitted,  # ✅ Frontend needs this
                'wasReady': was_ready, # ✅ Frontend needs this
                'language': room_language
            }, room=request.sid)

            self.game_manager.emit_state_update(room_id)
            print(f"✅ Rejoin completed for player {player_id}")
            
        except Exception as e:
            print(f"❌ Error in rejoin_game: {e}")
            import traceback
            traceback.print_exc()
            emit('reconnect_player', {'success': False, 'message': 'An error occurred during rejoin.'})