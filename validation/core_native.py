"""Multi-core native Libretro validation gate."""
import argparse
import ctypes as c
import faulthandler
import json
from pathlib import Path
import tempfile

CORE_CONFIGS = {
    "cemu": {
        "dll": "cemu_libretro.dll",
        "name_matches": ["cemu"],
        "extension": "wud",
        "marker": "CEMU_NATIVE_OK",
    },
    "ppsspp": {
        "dll": "ppsspp_libretro.dll",
        "name_matches": ["ppsspp"],
        "extension": "iso",
        "marker": "PPSSPP_NATIVE_OK",
    },
    "flycast": {
        "dll": "flycast_libretro.dll",
        "name_matches": ["flycast"],
        "extension": "cdi",
        "marker": "FLYCAST_NATIVE_OK",
    },
    "pcsx2": {
        "dll": "pcsx2_libretro.dll",
        "name_matches": ["pcsx2", "lrps2"],
        "extension": "elf",
        "marker": "PCSX2_NATIVE_OK",
        "renderer_option": ("pcsx2_renderer", b"Software"),
    },
    "eden": {
        "dll": "eden_libretro.dll",
        "name_matches": ["eden"],
        "extension": "nsp",
        "marker": "EDEN_NATIVE_OK",
    },
    "rpcs3": {
        "dll": "rpcs3/rpcs3_libretro.dll",
        "fallback_dll": "rpcs3_libretro.dll",
        "name_matches": ["rpcs3"],
        "extension": "elf",
        "marker": "RPCS3_NATIVE_OK",
    },
    "shadps4": {
        "dll": "shadps4_libretro.dll",
        "name_matches": ["shadps4"],
        "extension": "pkg",
        "marker": "SHADPS4_NATIVE_OK",
    },
}


class Info(c.Structure):
    _fields_ = [("name", c.c_char_p), ("version", c.c_char_p), ("extensions", c.c_char_p),
                ("fullpath", c.c_bool), ("block_extract", c.c_bool)]


class Variable(c.Structure):
    _fields_ = [("key", c.c_char_p), ("value", c.c_char_p)]


class Game(c.Structure):
    _fields_ = [("path", c.c_char_p), ("data", c.c_void_p), ("size", c.c_size_t), ("meta", c.c_char_p)]


def find_entry_dll(package, core):
    if package.is_file() and package.suffix.lower() == ".dll":
        return package
    cfg = CORE_CONFIGS.get(core, {})
    primary = package / cfg.get("dll", f"{core}_libretro.dll")
    if primary.is_file():
        return primary
    if "fallback_dll" in cfg:
        fallback = package / cfg["fallback_dll"]
        if fallback.is_file():
            return fallback
    matches = list(package.rglob(f"{core}_libretro.dll"))
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"Could not find entry library for {core} in {package}")


def _probe(core, package, trace):
    cfg = CORE_CONFIGS.get(core)
    if not cfg:
        raise ValueError(f"Unsupported core target: {core}")
    entry = find_entry_dll(package, core)
    trace("load_library")
    library = c.CDLL(str(entry.resolve()), winmode=0x00001100)
    library.retro_api_version.restype = c.c_uint
    trace("retro_api_version")
    assert library.retro_api_version() == 1, "Libretro ABI mismatch"
    library.retro_get_system_info.argtypes = [c.POINTER(Info)]
    info = Info()
    trace("retro_get_system_info")
    library.retro_get_system_info(c.byref(info))
    name_bytes = (info.name or b"").lower()
    assert any(m.encode() in name_bytes for m in cfg["name_matches"]), f"Core identity mismatch: {info.name}"
    library.retro_load_game.argtypes, library.retro_load_game.restype = [c.POINTER(Game)], c.c_bool
    environment_type = c.CFUNCTYPE(c.c_bool, c.c_uint, c.c_void_p)
    video_type = c.CFUNCTYPE(None, c.c_void_p, c.c_uint, c.c_uint, c.c_size_t)
    audio_type = c.CFUNCTYPE(None, c.c_int16, c.c_int16)
    batch_type = c.CFUNCTYPE(c.c_size_t, c.c_void_p, c.c_size_t)
    poll_type = c.CFUNCTYPE(None)
    input_type = c.CFUNCTYPE(c.c_int16, c.c_uint, c.c_uint, c.c_uint, c.c_uint)
    # Libretro's printf callback is variadic. On Windows x64 the fixed prefix
    # uses the same calling convention; ignore formatting arguments in the probe.
    log_type = c.CFUNCTYPE(None, c.c_int, c.c_char_p)
    log_callback = log_type(lambda level, message: None)
    callbacks = [video_type(lambda *args: None), audio_type(lambda *args: None),
                 batch_type(lambda data, frames: frames), poll_type(lambda: None), input_type(lambda *args: 0)]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        system, saves = root / "system", root / "saves"
        system.mkdir(); saves.mkdir()
        system_bytes, save_bytes = str(system).encode(), str(saves).encode()
        variables, errors = {}, []

        def environment(command, data):
            try:
                if command == 27:
                    c.cast(data, c.POINTER(c.c_void_p))[0] = c.cast(log_callback, c.c_void_p).value
                    return True
                if command in (9, 31):
                    c.cast(data, c.POINTER(c.c_char_p))[0] = system_bytes if command == 9 else save_bytes
                    return True
                if command == 16:
                    values = c.cast(data, c.POINTER(Variable))
                    for index in range(256):
                        item = values[index]
                        if not item.key: break
                        variables[item.key] = item.value.split(b";", 1)[-1].strip().split(b"|")[0]
                    if "renderer_option" in cfg:
                        key, val = cfg["renderer_option"]
                        variables[key.encode()] = val
                    return True
                if command == 15:
                    item = c.cast(data, c.POINTER(Variable)).contents
                    item.value = variables.get(item.key)
                    return bool(item.value)
                if command == 17:
                    c.cast(data, c.POINTER(c.c_bool))[0] = False
                    return True
                # This headless probe has no hardware context or GL/Vulkan
                # function pointers. Claiming SET_HW_RENDER support is invalid.
                return command in (10, 35)
            except Exception as exc:
                errors.append(type(exc).__name__)
                return False

        env = environment_type(environment)
        library.retro_set_environment.argtypes = [environment_type]
        setters = ("retro_set_video_refresh", "retro_set_audio_sample", "retro_set_audio_sample_batch",
                   "retro_set_input_poll", "retro_set_input_state")
        for cycle in range(3):
            trace(f"cycle={cycle + 1} retro_set_environment")
            library.retro_set_environment(env)
            for name, callback in zip(setters, callbacks):
                trace(f"cycle={cycle + 1} {name}")
                getattr(library, name).argtypes = [type(callback)]
                getattr(library, name)(callback)
            trace(f"cycle={cycle + 1} retro_init")
            library.retro_init()
            trace(f"cycle={cycle + 1} retro_load_game(null)")
            assert not library.retro_load_game(None), "Empty content unexpectedly accepted"
            dummy_file = root / f"invalid.{cfg['extension']}"
            dummy_file.write_bytes(b"\x7fELF" + b"\0" * 60)
            trace(f"cycle={cycle + 1} retro_load_game(invalid)")
            assert not library.retro_load_game(c.byref(Game(str(dummy_file).encode(), None, 0, None))), "Invalid content unexpectedly accepted"
            trace(f"cycle={cycle + 1} retro_unload_game")
            library.retro_unload_game()
            trace(f"cycle={cycle + 1} retro_deinit")
            library.retro_deinit()
        assert not errors, "Frontend environment callback failed"
    return {"passed": True, "scope": "identity/options/initialization/rejected-content/cleanup",
            "cycles": 3, "gameplayTested": False, "core": core}


def probe(core, package, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        def trace(step):
            stream.write("CORE_NATIVE_STEP: " + step + "\n")
            stream.flush()

        faulthandler.enable(file=stream, all_threads=True)
        try:
            report = _probe(core, package, trace)
        finally:
            faulthandler.disable()
        stream.write(json.dumps(report) + f"\n{CORE_CONFIGS[core]['marker']}\nCORE_NATIVE_OK\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", default="pcsx2")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    probe(args.core, args.package, args.log)
