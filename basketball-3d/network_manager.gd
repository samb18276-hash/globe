extends Node

const PORT = 7777
const BROADCAST_PORT = 7778
const MAX_PLAYERS = 4

signal player_joined(id, username)
signal player_left(id)
signal all_players_ready
signal connection_failed
signal code_found(host_ip)

var players = {}
var local_username = "Player"
var is_host = false
var room_code = ""

var _broadcaster: PacketPeerUDP
var _listener: PacketPeerUDP
var _broadcast_timer: Timer
var _search_timer: Timer
var _searching_code = ""
var _is_searching = false

func _ready():
	load_username()
	multiplayer.peer_connected.connect(_on_peer_connected)
	multiplayer.peer_disconnected.connect(_on_peer_disconnected)
	multiplayer.connected_to_server.connect(_on_connected_to_server)
	multiplayer.connection_failed.connect(_on_connection_failed)

func load_username():
	var file = FileAccess.open("user://username.txt", FileAccess.READ)
	if file:
		local_username = file.get_line().strip_edges()
		file.close()

func save_username(name: String):
	local_username = name
	var file = FileAccess.open("user://username.txt", FileAccess.WRITE)
	if file:
		file.store_line(name)
		file.close()

func generate_code() -> String:
	var code = ""
	for i in 8:
		code += str(randi() % 10)
	return code

func host_game() -> bool:
	room_code = generate_code()
	is_host = true
	var peer = ENetMultiplayerPeer.new()
	if peer.create_server(PORT, MAX_PLAYERS) != OK:
		return false
	multiplayer.multiplayer_peer = peer
	players[1] = { "username": local_username, "ready": true }
	emit_signal("player_joined", 1, local_username)
	_start_broadcasting()
	return true

func _start_broadcasting():
	_broadcaster = PacketPeerUDP.new()
	_broadcaster.set_broadcast_enabled(true)
	_broadcaster.bind(0)
	_broadcast_timer = Timer.new()
	_broadcast_timer.wait_time = 1.0
	_broadcast_timer.timeout.connect(_send_broadcast)
	add_child(_broadcast_timer)
	_broadcast_timer.start()
	_send_broadcast()

func _send_broadcast():
	var msg = ("BBALL:%s:%d" % [room_code, PORT]).to_utf8_buffer()
	_broadcaster.set_dest_address("255.255.255.255", BROADCAST_PORT)
	_broadcaster.put_packet(msg)

func search_by_code(code: String):
	_searching_code = code
	_is_searching = true
	_listener = PacketPeerUDP.new()
	if _listener.bind(BROADCAST_PORT) != OK:
		emit_signal("connection_failed")
		return
	_search_timer = Timer.new()
	_search_timer.wait_time = 6.0
	_search_timer.one_shot = true
	_search_timer.timeout.connect(_on_search_timeout)
	add_child(_search_timer)
	_search_timer.start()

func _process(_delta):
	if not _is_searching or not _listener:
		return
	while _listener.get_available_packet_count() > 0:
		var packet = _listener.get_packet()
		var msg = packet.get_string_from_utf8()
		var parts = msg.split(":")
		if parts.size() == 3 and parts[0] == "BBALL" and parts[1] == _searching_code:
			var host_ip = _listener.get_packet_ip()
			_stop_searching()
			join_game(host_ip)
			emit_signal("code_found", host_ip)
			return

func _stop_searching():
	_is_searching = false
	if _listener:
		_listener.close()
		_listener = null
	if _search_timer and is_instance_valid(_search_timer):
		_search_timer.queue_free()
		_search_timer = null

func _on_search_timeout():
	_stop_searching()
	emit_signal("connection_failed")

func join_game(ip: String) -> bool:
	is_host = false
	var peer = ENetMultiplayerPeer.new()
	if peer.create_client(ip, PORT) != OK:
		emit_signal("connection_failed")
		return false
	multiplayer.multiplayer_peer = peer
	return true

func disconnect_from_game():
	if _broadcast_timer and is_instance_valid(_broadcast_timer):
		_broadcast_timer.queue_free()
	if _broadcaster:
		_broadcaster.close()
	_stop_searching()
	multiplayer.multiplayer_peer = null
	players.clear()
	is_host = false
	room_code = ""

func set_ready(state: bool):
	players[multiplayer.get_unique_id()]["ready"] = state
	rpc("sync_ready", multiplayer.get_unique_id(), state)
	_check_all_ready()

func _check_all_ready():
	if not is_host or players.size() < 2:
		return
	for p in players.values():
		if not p["ready"]:
			return
	emit_signal("all_players_ready")

func _on_peer_connected(id):
	rpc_id(id, "register_player", multiplayer.get_unique_id(), local_username)

func _on_peer_disconnected(id):
	players.erase(id)
	emit_signal("player_left", id)

func _on_connected_to_server():
	rpc("register_player", multiplayer.get_unique_id(), local_username)

func _on_connection_failed():
	emit_signal("connection_failed")

@rpc("any_peer", "reliable")
func register_player(id, username):
	if not players.has(id):
		players[id] = { "username": username, "ready": false }
		emit_signal("player_joined", id, username)
	if is_host:
		for existing_id in players:
			if existing_id != id:
				rpc_id(id, "register_player", existing_id, players[existing_id]["username"])
				rpc_id(id, "sync_ready", existing_id, players[existing_id]["ready"])

@rpc("any_peer", "reliable")
func sync_ready(id, state):
	if players.has(id):
		players[id]["ready"] = state
	_check_all_ready()

@rpc("call_local", "reliable")
func start_game():
	get_tree().change_scene_to_file("res://main.tscn")
