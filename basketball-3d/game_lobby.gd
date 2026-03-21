extends Control

@onready var player_list = $VBox/PlayerList
@onready var start_button = $VBox/StartButton
@onready var ready_button = $VBox/ReadyButton
@onready var status_label = $VBox/StatusLabel
@onready var code_label = $VBox/CodeLabel

var is_ready = false

func _ready():
	NetworkManager.player_joined.connect(_on_player_joined)
	NetworkManager.player_left.connect(_on_player_left)
	NetworkManager.all_players_ready.connect(_on_all_ready)

	start_button.visible = NetworkManager.is_host
	ready_button.visible = not NetworkManager.is_host

	if NetworkManager.is_host:
		code_label.text = "GAME CODE: " + NetworkManager.room_code
		code_label.visible = true
	else:
		code_label.visible = false

	start_button.pressed.connect(_on_start_pressed)
	ready_button.pressed.connect(_on_ready_pressed)
	$VBox/LeaveButton.pressed.connect(_on_leave_pressed)

	_refresh_player_list()

func _refresh_player_list():
	for child in player_list.get_children():
		child.free()
	for id in NetworkManager.players:
		var p = NetworkManager.players[id]
		var label = Label.new()
		var ready_text = "[READY]" if p["ready"] else "[NOT READY]"
		label.text = p["username"] + "   " + ready_text
		label.add_theme_font_size_override("font_size", 24)
		label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		player_list.add_child(label)

func _on_player_joined(_id, username):
	_refresh_player_list()
	status_label.text = username + " joined!"

func _on_player_left(_id):
	_refresh_player_list()
	status_label.text = "A player left."

func _on_ready_pressed():
	is_ready = not is_ready
	NetworkManager.set_ready(is_ready)
	ready_button.text = "UNREADY" if is_ready else "READY"
	_refresh_player_list()

func _on_all_ready():
	start_button.disabled = false
	status_label.text = "All players ready! Host can start."

func _on_start_pressed():
	NetworkManager.rpc("start_game")

func _on_leave_pressed():
	NetworkManager.disconnect_from_game()
	get_tree().change_scene_to_file("res://lobby.tscn")
