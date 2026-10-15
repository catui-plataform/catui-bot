"""Prepare the pinned CATui frontend used by headless compatibility gates."""
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def prepare():
    config = json.loads((ROOT / "config/frontend.json").read_text())
    root = (ROOT / "runtime-tools").resolve()
    root.mkdir(exist_ok=True)
    stamp = root / "ready.json"
    if stamp.exists():
        previous = json.loads(stamp.read_text())
        if previous.get("config") == config and all((root / name).is_file() and
                hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                for name, digest in previous["files"].items()):
            return

    engine = root / "godot"
    engine.mkdir(exist_ok=True)
    archive = engine / "godot.zip"
    with urllib.request.urlopen(config["godot_url"], timeout=60) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != config["godot_sha256"]:
        raise ValueError("Godot archive checksum mismatch")
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            (engine / member.filename).resolve().relative_to(engine.resolve())
        package.extractall(engine)
    archive.unlink()
    frontend = root / "frontend"
    frontend.mkdir(exist_ok=True)
    library = frontend / "catui_core.dll"
    with urllib.request.urlopen(config["frontend_url"], timeout=60) as response, library.open("wb") as output:
        shutil.copyfileobj(response, output)
    if hashlib.sha256(library.read_bytes()).hexdigest() != config["frontend_sha256"]:
        raise ValueError("CATui frontend checksum mismatch")
    libraries = list((root / "frontend").glob("*.dll"))
    engines = list(engine.glob("*console.exe"))
    if len(libraries) != 1 or len(engines) != 1:
        raise ValueError("Expected one frontend library and one Godot console executable")
    executables = list(engine.glob("*.exe"))
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in [*libraries, *executables]}
    stamp.write_text(json.dumps({"config": config, "files": files}, indent=2) + "\n")


if __name__ == "__main__":
    prepare()
