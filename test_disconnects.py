"""
Kellak Lies - Disconnect & Reconnect Stress Tester
Run with: python test_disconnects.py
Requires: pip install python-socketio[client]
"""

import socketio
import time

BASE_URL = "http://localhost:5000"
ROOM_ID = "TEST123"
RESULTS = []

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def make_player(name):
    sio = socketio.Client()
    state = {"phase": None, "id": None, "name": name, "events": [], "role": None}

    @sio.on("join_confirmation")
    def on_joined(data):
        state["id"] = data.get("playerId")
        log(f"✅ {name} joined with ID {state['id']}")

    @sio.on("update_game_state")
    def on_state(data):
        new_phase = data.get("phase")
        if new_phase != state["phase"]:
            log(f"📢 {name} sees phase: {state['phase']} → {new_phase}")
            state["phase"] = new_phase
            state["events"].append(new_phase)

    @sio.on("personal_game_info")
    def on_personal(data):
        state["role"] = data.get("role")
        state["question"] = data.get("question")

    @sio.on("error_event")
    def on_error(data):
        log(f"❌ {name} error_event: {data}")

    @sio.on("error")
    def on_error2(data):
        log(f"❌ {name} error: {data}")

    @sio.on("reconnect_player")
    def on_reconnect(data):
        log(f"🔄 {name} reconnect result: {data.get('success')} - {data.get('message', '')}")

    sio.connect(BASE_URL, transports=["websocket"])
    return sio, state


def run_test(test_name, test_fn):
    log(f"\n{'='*50}")
    log(f"TEST: {test_name}")
    log(f"{'='*50}")
    try:
        result = test_fn()
        status = "✅ PASS" if result else "❌ FAIL"
        RESULTS.append((test_name, result))
        log(f"{status}: {test_name}")
    except Exception as e:
        log(f"❌ ERROR: {test_name} — {e}")
        import traceback; traceback.print_exc()
        RESULTS.append((test_name, False))


# ─────────────────────────────────────────────
# TEST 1: Disconnect during question phase
# ─────────────────────────────────────────────
def test_disconnect_during_question():
    room = ROOM_ID + "_Q"
    players = []
    for i, name in enumerate(["Alice", "Bob", "Charlie"]):
        sio, state = make_player(name)
        players.append((sio, state))
        if i == 0:
            sio.emit("create_room", {"roomId": room, "name": name, "avatar": "x", "language": "en"})
        else:
            sio.emit("join_room", {"roomId": room, "name": name, "avatar": "x", "language": "en"})
        time.sleep(0.5)

    time.sleep(0.5)

    for sio, state in players:
        if not state["id"]:
            log(f"❌ {state['name']} never got a player ID — join failed")
            for s, _ in players:
                try: s.disconnect()
                except: pass
            return False

    alice_sio, alice_state = players[0]
    alice_sio.emit("start_game", {
        "roomId": room,
        "playerId": alice_state["id"],
        "settings": {"totalRounds": 1, "playerCount": 3, "discussTime": 30, "answerTime": 30, "gameMode": "normal"}
    })
    time.sleep(1.5)

    # Alice and Bob submit, Charlie does NOT
    for sio, state in players[:2]:
        sio.emit("submit_answer", {"roomId": room, "playerId": state["id"], "answer": "test answer"})
        time.sleep(0.3)

    log("💥 Charlie disconnecting without submitting...")
    players[2][0].disconnect()
    time.sleep(2)

    alice_phase = alice_state["phase"]
    bob_phase = players[1][1]["phase"]
    log(f"Alice phase: {alice_phase}, Bob phase: {bob_phase}")
    success = alice_phase == "voting" and bob_phase == "voting"

    for sio, _ in players[:2]:
        try: sio.disconnect()
        except: pass
    return success


# ─────────────────────────────────────────────
# TEST 2: Disconnect during voting phase
# ─────────────────────────────────────────────
def test_disconnect_during_voting():
    room = ROOM_ID + "_V"
    players = []
    for i, name in enumerate(["Dave", "Eve", "Frank"]):
        sio, state = make_player(name)
        players.append((sio, state))
        if i == 0:
            sio.emit("create_room", {"roomId": room, "name": name, "avatar": "x", "language": "en"})
        else:
            sio.emit("join_room", {"roomId": room, "name": name, "avatar": "x", "language": "en"})
        time.sleep(0.5)

    time.sleep(0.5)

    for sio, state in players:
        if not state["id"]:
            log(f"❌ {state['name']} never got a player ID — join failed")
            for s, _ in players:
                try: s.disconnect()
                except: pass
            return False

    dave_sio, dave_state = players[0]
    dave_sio.emit("start_game", {
        "roomId": room,
        "playerId": dave_state["id"],
        "settings": {"totalRounds": 1, "playerCount": 3, "discussTime": 30, "answerTime": 30, "gameMode": "normal"}
    })
    time.sleep(1.5)

    for sio, state in players:
        sio.emit("submit_answer", {"roomId": room, "playerId": state["id"], "answer": "test answer"})
        time.sleep(0.3)

    time.sleep(1)
    log(f"Dave phase after all answers: {dave_state['phase']}")

    # Dave and Eve click ready, Frank does NOT
    for sio, state in players[:2]:
        sio.emit("ready_to_vote", {"roomId": room, "playerId": state["id"]})
        time.sleep(0.3)

    log("💥 Frank disconnecting without clicking ready...")
    players[2][0].disconnect()
    time.sleep(2)

    dave_phase = dave_state["phase"]
    eve_phase = players[1][1]["phase"]
    log(f"Dave phase: {dave_phase}, Eve phase: {eve_phase}")
    success = dave_phase == "vote_selection" and eve_phase == "vote_selection"

    for sio, _ in players[:2]:
        try: sio.disconnect()
        except: pass
    return success


# ─────────────────────────────────────────────
# TEST 3: Rejoin within 30 seconds
# 3 players so room survives the disconnect
# ─────────────────────────────────────────────
def test_rejoin_within_window():
    room = ROOM_ID + "_R"
    players = []
    for i, name in enumerate(["Grace", "Hank", "Ivy"]):
        sio, state = make_player(name)
        players.append((sio, state))
        if i == 0:
            sio.emit("create_room", {"roomId": room, "name": name, "avatar": "x", "language": "en"})
        else:
            sio.emit("join_room", {"roomId": room, "name": name, "avatar": "x", "language": "en"})
        time.sleep(0.5)

    time.sleep(0.5)

    for sio, state in players:
        if not state["id"]:
            log(f"❌ {state['name']} never got a player ID — join failed")
            for s, _ in players:
                try: s.disconnect()
                except: pass
            return False

    grace_sio, grace_state = players[0]
    grace_sio.emit("start_game", {
        "roomId": room,
        "playerId": grace_state["id"],
        "settings": {"totalRounds": 1, "playerCount": 3, "discussTime": 60, "answerTime": 60, "gameMode": "normal"}
    })
    time.sleep(1.5)

    # Hank disconnects (Grace and Ivy still active — room survives)
    hank_id = players[1][1]["id"]
    log(f"💥 Hank (ID: {hank_id}) disconnecting...")
    players[1][0].disconnect()
    time.sleep(1)

    log("🔄 Hank attempting rejoin...")
    rejoin_result = {"success": None}
    new_sio = socketio.Client()

    @new_sio.on("reconnect_player")
    def on_reconnect(data):
        rejoin_result["success"] = data.get("success")
        log(f"Rejoin response: {data.get('success')} — {data.get('message', '')}")

    @new_sio.on("error_event")
    def on_error(data):
        rejoin_result["success"] = False
        log(f"Rejoin error_event: {data}")

    new_sio.connect(BASE_URL, transports=["websocket"])
    new_sio.emit("rejoin_game", {
        "roomId": room,
        "playerId": hank_id,
        "timeStamp": int(time.time() * 1000),
        "language": "en"
    })
    time.sleep(2)

    success = rejoin_result["success"] == True
    log(f"Rejoin success: {rejoin_result['success']}")

    for sio in [grace_sio, new_sio, players[2][0]]:
        try: sio.disconnect()
        except: pass
    return success


# ─────────────────────────────────────────────
# TEST 4: Rejoin AFTER 30 second window expires
# 3 players so room survives the disconnect
# ─────────────────────────────────────────────
def test_rejoin_after_window_expired():
    room = ROOM_ID + "_E"
    players = []
    for i, name in enumerate(["Iris", "Jake", "Kim"]):
        sio, state = make_player(name)
        players.append((sio, state))
        if i == 0:
            sio.emit("create_room", {"roomId": room, "name": name, "avatar": "x", "language": "en"})
        else:
            sio.emit("join_room", {"roomId": room, "name": name, "avatar": "x", "language": "en"})
        time.sleep(0.5)

    time.sleep(0.5)

    for sio, state in players:
        if not state["id"]:
            log(f"❌ {state['name']} never got a player ID — join failed")
            for s, _ in players:
                try: s.disconnect()
                except: pass
            return False

    iris_sio, iris_state = players[0]
    iris_sio.emit("start_game", {
        "roomId": room,
        "playerId": iris_state["id"],
        "settings": {"totalRounds": 1, "playerCount": 3, "discussTime": 60, "answerTime": 60, "gameMode": "normal"}
    })
    time.sleep(1.5)

    jake_id = players[1][1]["id"]
    log(f"💥 Jake (ID: {jake_id}) disconnecting...")
    players[1][0].disconnect()

    log("⏳ Waiting 32 seconds for window to expire...")
    time.sleep(32)

    rejoin_result = {"success": None, "message": None}
    new_sio = socketio.Client()

    @new_sio.on("reconnect_player")
    def on_reconnect(data):
        rejoin_result["success"] = data.get("success")
        rejoin_result["message"] = data.get("message")
        log(f"Rejoin response: {data.get('success')} — {data.get('message', '')}")

    @new_sio.on("error_event")
    def on_error(data):
        rejoin_result["success"] = False
        rejoin_result["message"] = data.get("message")
        log(f"Expected rejection: {data}")

    new_sio.connect(BASE_URL, transports=["websocket"])
    new_sio.emit("rejoin_game", {
        "roomId": room,
        "playerId": jake_id,
        "timeStamp": int(time.time() * 1000),
        "language": "en"
    })
    time.sleep(2)

    success = rejoin_result["success"] == False
    log(f"Expected failure, got success={rejoin_result['success']}, message={rejoin_result['message']}")

    for sio in [iris_sio, new_sio, players[2][0]]:
        try: sio.disconnect()
        except: pass
    return success


# ─────────────────────────────────────────────
# RUN ALL TESTS
# ─────────────────────────────────────────────
if __name__ == "__main__":
    log("🚀 Starting Kellak Lies disconnect tests")
    log("Make sure your backend is running on localhost:5000\n")

    time.sleep(1)

    run_test("Disconnect during question phase → auto-advance to voting", test_disconnect_during_question)
    time.sleep(2)
    run_test("Disconnect during voting phase → auto-advance to vote_selection", test_disconnect_during_voting)
    time.sleep(2)
    run_test("Rejoin within 30 second window", test_rejoin_within_window)
    time.sleep(2)
    run_test("Rejoin after 30 second window → correctly rejected", test_rejoin_after_window_expired)

    log(f"\n{'='*50}")
    log("RESULTS SUMMARY")
    log(f"{'='*50}")
    passed = sum(1 for _, r in RESULTS if r)
    for name, result in RESULTS:
        log(f"{'✅' if result else '❌'} {name}")
    log(f"\n{passed}/{len(RESULTS)} tests passed")