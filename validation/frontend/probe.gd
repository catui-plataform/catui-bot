extends SceneTree


func _initialize() -> void:
	_run.call_deferred()


func _fail(message: String) -> void:
	printerr("CATUI_COMPATIBILITY_FAIL: ", message)
	quit(1)


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() < 1 or not ClassDB.class_exists("LibretroSession"):
		_fail("Core argument or CATui LibretroSession is missing")
		return
	var core_path: String = args[0]
	var core_id := ""
	if args.size() >= 2:
		core_id = args[1].to_lower()
	else:
		var core_lower := core_path.to_lower()
		if core_lower.contains("pcsx2") or core_lower.contains("lrps2"):
			core_id = "pcsx2"
		elif core_lower.contains("eden"):
			core_id = "eden"
		elif core_lower.contains("rpcs3"):
			core_id = "rpcs3"
		elif core_lower.contains("shadps4"):
			core_id = "shadps4"
		else:
			core_id = "generic"

	DirAccess.make_dir_recursive_absolute("user://system")
	DirAccess.make_dir_recursive_absolute("user://saves")

	var ext := "elf"
	var dummy_bytes := PackedByteArray([127, 69, 76, 70, 0, 0, 0, 0])
	if core_id == "eden":
		ext = "nsp"
		dummy_bytes = PackedByteArray([0, 0, 0, 0, 0, 0, 0, 0])
	elif core_id == "shadps4":
		ext = "pkg"
		dummy_bytes = PackedByteArray([127, 67, 78, 84, 0, 0, 0, 0])
	elif core_id == "rpcs3":
		ext = "elf"
		dummy_bytes = PackedByteArray([127, 69, 76, 70, 0, 0, 0, 0])
	elif core_id == "ppsspp":
		ext = "iso"
		dummy_bytes = PackedByteArray([0, 0, 0, 0, 0, 0, 0, 0])
	elif core_id == "flycast":
		ext = "cdi"
		dummy_bytes = PackedByteArray([0, 0, 0, 0, 0, 0, 0, 0])
	elif core_id == "cemu":
		ext = "wud"
		dummy_bytes = PackedByteArray([0, 0, 0, 0, 0, 0, 0, 0])

	var content := "user://invalid." + ext
	var file := FileAccess.open(content, FileAccess.WRITE)
	file.store_buffer(dummy_bytes)
	file.close()

	for cycle in 3:
		var session = ClassDB.instantiate("LibretroSession")
		root.add_child(session)
		session.system_directory = ProjectSettings.globalize_path("user://system")
		session.save_directory = ProjectSettings.globalize_path("user://saves")
		if session.has_method("set_core_lifecycle_profile"):
			session.set_core_lifecycle_profile(core_id)
		if core_id == "pcsx2" and session.has_method("set_core_option"):
			session.set_core_option("pcsx2_renderer", "Software")
		var target := TextureRect.new()
		root.add_child(target)
		var loaded: bool = session.start(core_path, ProjectSettings.globalize_path(content), target)
		var error: String = session.get_last_error()
		session.stop()
		var deadline := Time.get_ticks_msec() + 5000
		while (session.is_running() or session.is_starting()) and Time.get_ticks_msec() < deadline:
			await process_frame
		var running: bool = session.is_running()
		session.free()
		target.free()
		if loaded or running or not error.contains("retro_load_game rejected"):
			_fail("Expected clean rejection through the actual core; got: " + error)
			return
		print("CATUI_COMPATIBILITY_CYCLE_OK=", cycle + 1)
	print("CATUI_COMPATIBILITY_OK: initialization/rejected-content/cleanup; gameplay not tested")
	quit(0)
