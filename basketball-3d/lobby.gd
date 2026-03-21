extends Control

@onready var username_input = $VBox/UsernameInput
@onready var code_input = $VBox/JoinBox/CodeInput
@onready var join_box = $VBox/JoinBox
@onready var status_label = $VBox/StatusLabel

func _ready():
	username_input.text = NetworkManager.local_username
	join_box.visible = false
	$VBox/HostButton.pressed.connect(_on_host_pressed)
	$VBox/JoinButton.pressed.connect(_on_join_toggle)
	$VBox/JoinBox/ConnectButton.pressed.connect(_on_connect_pressed)
	$VBox/QuitButton.pressed.connect(func(): get_tree().quit())
	NetworkManager.connection_failed.connect(_on_connection_failed)

func _on_host_pressed():
	var uname = username_input.text.strip_edges()
	if uname == "":
		status_label.text = "Enter a username first!"
		return
	NetworkManager.save_username(uname)
	if NetworkManager.host_game():
		get_tree().change_scene_to_file("res://game_lobby.tscn")
	else:
		status_label.text = "Failed to host. Try again."

func _on_join_toggle():
	join_box.visible = !join_box.visible
	if join_box.visible:
		code_input.grab_focus()

func _on_connect_pressed():
	var uname = username_input.text.strip_edges()
	if uname == "":
		status_label.text = "Enter a username first!"
		return
	var code = code_input.text.strip_edges()
	if code.length() != 8:
		status_label.text = "Code must be 8 digits!"
		return
	NetworkManager.save_username(uname)
	status_label.text = "Searching for game..."
	NetworkManager.code_found.connect(_on_code_found, CONNECT_ONE_SHOT)
	NetworkManager.search_by_code(code)

func _on_code_found(_host_ip):
	get_tree().change_scene_to_file("res://game_lobby.tscn")

func _on_connection_failed():
	status_label.text = "Game not found. Check the code and try again!"
