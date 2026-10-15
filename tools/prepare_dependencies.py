"""Install owner-pinned Windows dependency bundles, without secrets."""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import zipfile
import subprocess


def vcpkg_dependencies(core, bundle):
    commit = bundle["commit"]
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("vcpkg must be pinned to an exact commit")
    if bundle.get("triplet") != "x64-windows-static":
        raise ValueError("PCSX2 requires static x64 dependencies")
    root = (Path("dependencies") / core).resolve()
    checkout, installed = root / "vcpkg", root / "installed"
    manifest = Path(__file__).resolve().parents[1] / "config/vcpkg" / core
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("GITHUB_", "ACTIONS_"))
                   and not any(word in k.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "SIGNING", "API_KEY"))}

    def run(args, timeout):
        result = subprocess.run([str(x) for x in args], env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
        with Path("build.log").open("ab") as log:
            log.write(result.stdout[-500000:])
        if result.returncode:
            raise RuntimeError(f"Dependency setup failed: {Path(args[0]).name}; see build.log")

    root.mkdir(parents=True, exist_ok=True)
    if not (checkout / ".git").exists():
        run(["git", "init", checkout], 60)
        run(["git", "-C", checkout, "remote", "add", "origin", "https://github.com/microsoft/vcpkg.git"], 30)
    run(["git", "-C", checkout, "fetch", "--depth=1", "origin", commit], 300)
    run(["git", "-C", checkout, "checkout", "--detach", commit], 60)
    run(["cmd.exe", "/c", str(checkout / "bootstrap-vcpkg.bat"), "-disableMetrics"], 300)
    run([checkout / "vcpkg.exe", "install", "--triplet=x64-windows-static",
         "--x-manifest-root=" + str(manifest), "--x-install-root=" + str(installed),
         "--clean-after-build", "--no-print-usage"], 7200)
    return {"CMAKE_TOOLCHAIN_FILE": str(checkout / "scripts/buildsystems/vcpkg.cmake"),
            "CMAKE_PREFIX_PATH": str(installed / "x64-windows-static"),
            "VCPKG_INSTALLED_DIR": str(installed), "VCPKG_TARGET_TRIPLET": "x64-windows-static",
            "VCPKG_MANIFEST_DIR": str(manifest), "VCPKG_MANIFEST_INSTALL": "OFF"}


def prepare(core):
    if core in ("eden", "shadps4"):
        import shutil
        if not shutil.which("glslangValidator"):
            dest_dir = (Path("dependencies") / "tools" / "bin").resolve()
            dest_dir.mkdir(parents=True, exist_ok=True)
            validator = dest_dir / "glslangValidator.exe"
            glslang_exe = dest_dir / "glslang.exe"
            if not validator.exists():
                url = "https://github.com/KhronosGroup/glslang/releases/download/16.6.0/glslang-16.6.0-windows-x86_64-release.zip"
                archive = dest_dir / "glslang.zip"
                try:
                    with urllib.request.urlopen(url, timeout=60) as resp, archive.open("wb") as out:
                        out.write(resp.read())
                    with zipfile.ZipFile(archive) as z:
                        for member in z.namelist():
                            if member.endswith(".exe"):
                                target_name = Path(member).name
                                (dest_dir / target_name).write_bytes(z.read(member))
                    if not validator.exists() and glslang_exe.exists():
                        shutil.copy2(glslang_exe, validator)
                    elif not glslang_exe.exists() and validator.exists():
                        shutil.copy2(validator, glslang_exe)
                    archive.unlink(missing_ok=True)
                except Exception as exc:
                    print(f"Notice: could not download glslang release: {exc}")
            if dest_dir.exists():
                if str(dest_dir) not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = str(dest_dir) + os.pathsep + os.environ.get("PATH", "")
                if os.environ.get("GITHUB_PATH"):
                    try:
                        with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as gp:
                            gp.write(str(dest_dir) + "\n")
                    except Exception:
                        pass
    config = Path(__file__).resolve().parents[1] / "config/dependencies.json"
    bundles = json.loads(config.read_text()) if config.exists() else {}
    bundle = bundles.get(core)
    if not bundle:
        return {}  # A runner may already have its dependencies configured.
    if bundle.get("provider") == "vcpkg":
        return vcpkg_dependencies(core, bundle)
    url, digest = bundle["url"], bundle["sha256"]
    if not url.startswith("https://github.com/catui-plataform/catui-bot/releases/download/"):
        raise ValueError("Dependency bundle must be an owner-maintained CATui Bot release")
    root = Path("dependencies") / core
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "dependencies.zip"
    actual = hashlib.sha256()
    deadline = time.monotonic() + 600
    with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            if output.tell() > 2 * 1024**3 or time.monotonic() > deadline:
                raise ValueError("Dependency bundle download exceeds limit")
            actual.update(chunk)
            output.write(chunk)
    if actual.hexdigest() != digest:
        raise ValueError("Dependency bundle SHA-256 mismatch")
    with zipfile.ZipFile(archive) as package:
        if sum(info.file_size for info in package.infolist()) > 10 * 1024**3:
            raise ValueError("Dependency bundle extraction exceeds limit")
        for info in package.infolist():
            destination = (root / info.filename).resolve()
            destination.relative_to(root.resolve())
            if ":" in info.filename or "\\" in info.filename or info.filename.startswith("/"):
                raise ValueError("Unsafe dependency archive path")
        package.extractall(root)
    archive.unlink()
    result = {}
    for name in ("CMAKE_PREFIX_PATH", "CMAKE_TOOLCHAIN_FILE", "Qt6_DIR", "LLVM_DIR", "VULKAN_SDK"):
        if name in bundle:
            location = (root / bundle[name]).resolve()
            location.relative_to(root.resolve())
            if not location.exists():
                raise ValueError(f"Dependency bundle is missing {name}")
            result[name] = str(location)
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    registry = json.loads((Path(__file__).resolve().parents[1] / "config/cores.json").read_text())
    parser.add_argument("--core", required=True, choices=registry)
    args = parser.parse_args()
    settings = prepare(args.core)
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as output:
            for name, value in settings.items():
                if "\n" in value or "\r" in value:
                    raise ValueError("Invalid dependency path")
                output.write(name + "=" + value + "\n")
