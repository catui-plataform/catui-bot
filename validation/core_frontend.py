"""Multi-core CATui frontend Libretro validation gate."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

try:
    from validation.core_native import find_entry_dll
except ImportError:
    from core_native import find_entry_dll

ROOT = Path(__file__).resolve().parents[1]


def probe(core, package, log):
    tools = ROOT / "runtime-tools"
    libraries = list((tools / "frontend").glob("*.dll"))
    engines = list((tools / "godot").glob("*console.exe"))
    if len(libraries) != 1 or len(engines) != 1:
        raise ValueError("Pinned frontend tools are not prepared")
    entry = find_entry_dll(package, core)
    log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        project = Path(folder)
        shutil.copytree(ROOT / "validation/frontend", project, dirs_exist_ok=True)
        shutil.copy2(libraries[0], project / "frontend.dll")
        (project / "frontend.gdextension").write_text(
            '[configuration]\nentry_symbol="godot_libretro_library_init"\ncompatibility_minimum="4.7"\n'
            '[libraries]\nwindows.release.x86_64="res://frontend.dll"\nwindows.debug.x86_64="res://frontend.dll"\n')
        # Force extension discovery before running a standalone SceneTree script.
        cache = project / ".godot/extension_list.cfg"
        cache.parent.mkdir()
        cache.write_text("res://frontend.gdextension\n")
        # Invoke the engine directly; its console companion launches a child.
        engine = engines[0].with_name(engines[0].name.replace("_console", ""))
        result = subprocess.run([
            str(engine), "--headless", "--max-fps", "60", "--quit-after", "1200", "--path", str(project),
            "--log-file", str(log.resolve()), "--script", "res://probe.gd", "--",
            str(entry.resolve()), core
        ], capture_output=True, timeout=90)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        with log.open("a", encoding="utf-8") as stream:
            stream.write("\n" + output)
        if result.returncode or "CATUI_COMPATIBILITY_OK" not in output or any(
                marker in output for marker in ("SCRIPT ERROR", "Parse Error", "CrashHandler")):
            raise RuntimeError(f"CATui frontend compatibility check failed for {core}; see its log")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", default="pcsx2")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    probe(args.core, args.package, args.log)
