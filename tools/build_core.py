"""Build in a disposable Windows runner. No publication credentials here."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
import struct
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catui_bot.config import ROOT
from catui_bot.security import validate_plan
from tools.repair import MAX_SOURCE, propose_repair
from tools.source_snapshot import snapshot
from tools.integration_fixes import apply_known_fixes

MAX_PACKAGE = 100 * 1024 * 1024
FORBIDDEN_PARTS = {".git", ".github", "catui-build", "catui-package", "node_modules", "__pycache__"}
ALLOWED_SUFFIXES = {".cpp", ".c", ".h", ".hpp", ".cmake"}


def is_editable_source(work, rel_path):
    if not rel_path or rel_path.startswith(("/", "\\")) or ":" in rel_path:
        return False
    normalized = rel_path.replace("\\", "/").lstrip("./")
    parts = Path(normalized).parts
    if not parts or ".." in parts or any(part in FORBIDDEN_PARTS for part in parts):
        return False
    try:
        target = (work / normalized).resolve()
        target.relative_to(work.resolve())
    except ValueError:
        return False
    if target.is_symlink() or not target.is_file():
        return False
    return target.suffix in ALLOWED_SUFFIXES or target.name == "CMakeLists.txt"


def extract_log_paths(log_text, work):
    work_posix = work.resolve().as_posix()
    found = []
    pattern = re.compile(r"([A-Za-z0-9_./\\+-]+(?:\.(?:cpp|c|h|hpp|cmake)|CMakeLists\.txt))")
    for raw in pattern.findall(log_text):
        candidate = raw.replace("\\", "/")
        if candidate.startswith(work_posix + "/"):
            candidate = candidate[len(work_posix) + 1:]
        elif "/core-work/" in candidate:
            candidate = candidate.split("/core-work/", 1)[1]
        elif candidate.startswith("/workspace/"):
            candidate = candidate[len("/workspace/"):]
        candidate = candidate.lstrip("./")
        if is_editable_source(work, candidate) and candidate not in found:
            found.append(candidate)
    return found


def prioritize_integration_paths(changed_files):
    def rank(path):
        norm = path.replace("\\", "/")
        if norm.startswith(("libretro/libretro-common/", "libretro/libretro-usb/")):
            return 2
        if (norm.startswith(("libretro/modern_", "src/eden_libretro/")) or norm in {"CMakeLists.txt", "libretro/CMakeLists.txt", "pcsx2/CMakeLists.txt"}
                or norm.startswith("cmake/")):
            return 0
        return 1

    return sorted(dict.fromkeys(changed_files), key=rank)


def apply_repairs(work, repairs, allowed_paths):
    applied = []
    for item in repairs:
        path = item["path"].replace("\\", "/")
        if path not in allowed_paths or len(item["content"]) > MAX_SOURCE:
            raise ValueError("Repair outside approved integration files")
        destination = (work / path).resolve()
        destination.relative_to(work.resolve())
        if any(part in FORBIDDEN_PARTS for part in destination.relative_to(work.resolve()).parts):
            raise ValueError("Repair path is not allowed")
        if destination.suffix not in ALLOWED_SUFFIXES and destination.name != "CMakeLists.txt":
            raise ValueError("Repair file type is not allowed")
        if re.search(r"-----BEGIN .*PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{20,}", item["content"]):
            raise ValueError("Credential pattern detected in repair")
        if re.search(r"^(?:<<<<<<< |=======\s*$|>>>>>>> )", item["content"], re.M):
            raise ValueError("Repair still contains merge conflict markers")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(item["content"], encoding="utf-8", newline="\n")
        command(["git", "add", "--", path], work, 30)
        applied.append(path)
    return applied


def apply_integration_patch(work, patch):
    if not patch.read_bytes().strip():
        return []  # Upstream-native Libretro cores need no initial integration patch.
    # Agent instructions are not emulator integration. An obsolete doc hunk can
    # otherwise abort the entire atomic apply before any source conflicts exist.
    excluded = ["AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md"]
    try:
        command(["git", "apply", "--3way", *["--exclude=" + p for p in excluded], str(patch.resolve())], work, 120)
    except RuntimeError as exc:
        conflicts = command(["git", "diff", "--name-only", "--diff-filter=U"], work, 60).splitlines()
        if not conflicts:
            raise RuntimeError(f"Integration patch aborted before source conflicts were available: {exc}") from exc
        return conflicts
    return []


def single_dll_check(path):
    data = path.read_bytes()
    offset = struct.unpack_from("<I", data, 0x3c)[0]
    if data[offset:offset + 4] != b"PE\0\0" or struct.unpack_from("<H", data, offset + 4)[0] != 0x8664:
        raise ValueError("PCSX2 must be a Windows x64 PE library")
    text = command(["dumpbin.exe", "/dependents", str(path.resolve())], Path.cwd(), 60)
    imports = {name.lower() for name in re.findall(r"^\s+([A-Za-z0-9_.-]+\.dll)\s*$", text, re.M | re.I)}
    system = {"kernel32.dll", "ntdll.dll", "user32.dll", "gdi32.dll", "gdi32full.dll", "advapi32.dll",
              "shell32.dll", "shlwapi.dll", "ole32.dll", "oleaut32.dll", "comdlg32.dll", "comctl32.dll",
              "winmm.dll", "version.dll", "ws2_32.dll", "iphlpapi.dll", "setupapi.dll", "cfgmgr32.dll",
              "imm32.dll", "hid.dll", "dinput8.dll", "xinput1_4.dll", "dwmapi.dll", "dxgi.dll",
              "d3d11.dll", "d3d12.dll", "d3dcompiler_47.dll", "opengl32.dll", "uxtheme.dll",
              "bcrypt.dll", "crypt32.dll", "secur32.dll", "psapi.dll", "pdh.dll", "winhttp.dll",
              "dbghelp.dll", "propsys.dll", "powrprof.dll", "avrt.dll", "msvcrt.dll", "ucrtbase.dll",
              "msimg32.dll", "winspool.drv", "normaliz.dll", "wintrust.dll", "netapi32.dll"}
    unexpected = imports - system - {name for name in imports if name.startswith(("api-ms-win-", "ext-ms-win-"))}
    if not imports or unexpected:
        raise ValueError("PCSX2 has unsupported DLL dependencies: " + ", ".join(sorted(unexpected)))
    return sorted(imports)


def command(args, cwd, timeout=1800):
    # No shell. Logs stay in the runner and never include environment dumps.
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("GITHUB_", "ACTIONS_"))
                   and not any(word in k.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "SIGNING", "API_KEY"))}
    result = subprocess.run([str(x) for x in args], cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout, env=environment)
    text = result.stdout.decode("utf-8", "replace")
    with open("build.log", "a", encoding="utf-8") as log:
        log.write(text.replace(str(Path.cwd()), "/workspace")[-500000:])
    if result.returncode:
        errors = [line.strip() for line in text.splitlines()
                  if re.search(r"(?:^error:|^fatal:|ninja: error:|CMake Error|error [A-Z]\d+:|validation failed:|Windows fatal exception|^\w*Error:)", line)][:6]
        detail = ": " + " | ".join(errors) if errors else "; see build.log"
        raise RuntimeError(f"{Path(args[0]).name} failed with exit {result.returncode}{detail}")
    return text.rstrip("\r\n")


def find_one(root, name):
    found = [p for p in root.rglob(name) if p.is_file()]
    # Prefer the configured Release output over dependency/debug directories.
    if len(found) != 1:
        releases = [p for p in found if "Release" in p.parts]
        if len(releases) == 1:
            return releases[0]
        raise ValueError(f"Expected exactly one output named {name}, found {len(found)}")
    return found[0]


def inspect_package(package, manifest):
    if package.stat().st_size >= MAX_PACKAGE:
        raise ValueError("Package exceeds 100 MiB; store download support must be extended before publishing")
    if manifest.get("packageType") == "archive":
        with zipfile.ZipFile(package) as archive:
            if archive.testzip() is not None or manifest["entryFilename"] not in archive.namelist():
                raise ValueError("Incomplete core archive")
            for name in archive.namelist():
                if name.startswith(("/", "\\")) or "\\" in name or ".." in Path(name).parts or ":" in name:
                    raise ValueError("Unsafe archive path")
                if not name.endswith("/") and not name.lower().endswith((".dll", ".exe", ".json", ".dat", ".bin", ".txt", ".pak", ".qm")) and not Path(name).name.startswith(("LICENSE", "COPYING")):
                    raise ValueError("Unexpected archive member")
    elif package.read_bytes()[:2] != b"MZ":
        raise ValueError("Core is not a Windows PE binary")
    return hashlib.sha256(package.read_bytes()).hexdigest()


def abi_check(path):
    # This runs in a separate process with a deadline; a broken DLL cannot hang the bot.
    library = ctypes.CDLL(str(path.resolve()))
    for name in ("retro_init", "retro_deinit", "retro_api_version", "retro_get_system_info",
                 "retro_load_game", "retro_unload_game", "retro_run", "retro_reset",
                 "retro_set_environment", "retro_set_video_refresh", "retro_set_audio_sample_batch",
                 "retro_set_input_poll", "retro_set_input_state", "retro_serialize", "retro_unserialize"):
        getattr(library, name)
    library.retro_api_version.restype = ctypes.c_uint
    if library.retro_api_version() != 1:
        raise ValueError("Unsupported Libretro ABI")


def load_recipe(core, source):
    path = source / "build.json"
    if not path.is_file():
        path = ROOT / "config/builds" / f"{core}.json"
    if not path.is_file():
        raise ValueError(f"No build recipe is available for {core}")
    return json.loads(path.read_text(encoding="utf-8"))


def build(plan, store, work, output):
    validate_plan(plan)
    registry = json.loads((ROOT / "config/cores.json").read_text())
    core = plan["core"]
    core_dir = store / "store/cores" / core
    source = core_dir / "source"
    metadata = json.loads((source / "upstream.json").read_text())
    recipe = load_recipe(core, source)
    if metadata["repository"] != registry[core]["repository"]:
        raise ValueError("Upstream repository mismatch")
    if not re.fullmatch(r"[a-f0-9]{40}", metadata["commit"]):
        raise ValueError("Invalid source baseline")
    patch = source / "integration.patch"
    if patch.is_file():
        patch.write_bytes(patch.read_bytes().replace(b"\r\n", b"\n"))
    ai_repair_enabled = bool(os.environ.get("DEEPSEEK_API_KEY")) and os.environ.get("AI_REPAIR_ENABLED", "true").lower() == "true"
    max_repair_attempts = max(1, min(50, int(os.environ.get("AI_REPAIR_MAX_ATTEMPTS", "12")))) if ai_repair_enabled else 0
    review = None
    if os.environ.get("REQUIRE_INTEGRATION_REVIEW", "false").lower() == "true":
        review = json.loads((ROOT / "review/core-review.json").read_text())
        patch_digest = hashlib.sha256(patch.read_bytes()).hexdigest()
        allowed_decisions = {"continue", "needs_review"} if ai_repair_enabled else {"continue"}
        if (review.get("decision") not in allowed_decisions or review.get("target") != plan["sha"]
                or review.get("base") != metadata["commit"] or review.get("patchSha256") != patch_digest):
            raise ValueError("Integration review does not match approved source")
    work.mkdir(parents=True, exist_ok=False)
    command(["git", "init"], work, 60)
    command(["git", "config", "core.autocrlf", "false"], work, 30)
    command(["git", "remote", "add", "origin", metadata["repository"]], work, 60)
    try:
        command(["git", "fetch", "--depth=1", "origin", plan["sha"], metadata["commit"]], work, 900)
    except RuntimeError:
        mirror = registry[core].get("mirror_repository")
        if not mirror:
            raise
        command(["git", "remote", "set-url", "origin", mirror], work, 60)
        command(["git", "fetch", "--depth=1", "origin", plan["sha"], metadata["commit"]], work, 900)
    command(["git", "checkout", "--detach", plan["sha"]], work, 120)
    command(["git", "config", "user.name", "CATui Bot"], work, 30)
    command(["git", "config", "user.email", "catui-bot@users.noreply.github.com"], work, 30)
    repaired_files = []
    repair_attempts = 0
    validation_repair_attempts = 0
    source_fix_files = []
    repair_history = []
    def repair_failure(exc, stage, conflicts=()):
        nonlocal repair_attempts, validation_repair_attempts
        validation = stage in {"abi", "validation"}
        while (validation_repair_attempts if validation else repair_attempts) < max_repair_attempts:
            if validation:
                validation_repair_attempts += 1
            else:
                repair_attempts += 1
            attempt = validation_repair_attempts if validation else repair_attempts
            print(f"In-runner AI {stage} repair attempt {attempt}/{max_repair_attempts}: {exc}", file=sys.stderr)
            ctx = failure_context(plan, store, work, review=review, stage=stage, error=str(exc))
            ctx["attempt"] = attempt
            ctx["previousAttempts"] = repair_history[-4:]
            Path("failure-context.json").write_text(json.dumps(ctx), encoding="utf-8")
            try:
                proposed = propose_repair(ctx)
                changed = [item for item in proposed
                           if (work / item["path"]).read_text(encoding="utf-8", errors="replace") != item["content"]]
                if not changed:
                    reason = ctx.get("proposalStatus", "no repair files returned") if not proposed else "proposal repeated the current source unchanged"
                    print(f"AI repair made no progress: {reason}; unresolved files: {', '.join(ctx['conflicts'])}", file=sys.stderr)
                    repair_history.append({"attempt": attempt, "stage": stage, "result": reason})
                    ctx["previousAttempts"] = repair_history[-4:]
                    Path("failure-context.json").write_text(json.dumps(ctx), encoding="utf-8")
                    continue
                for path in apply_repairs(work, changed, {f["path"] for f in ctx["files"]}):
                    if path not in repaired_files:
                        repaired_files.append(path)
                repair_history.append({"attempt": attempt, "stage": stage, "result": "applied", "files": [f["path"] for f in changed]})
                if conflicts:
                    conflicts = command(["git", "diff", "--name-only", "--diff-filter=U"], work, 60).splitlines()
                    if conflicts:
                        continue
                return
            except Exception as repair_exc:
                print(f"Repair proposal unavailable: {type(repair_exc).__name__}; retrying", file=sys.stderr)
                time.sleep(min(attempt * 2, 15))
        raise exc

    active_patch = patch
    checkpoint = ROOT / "core-recovery.json"
    if checkpoint.is_file():
        recovered = json.loads(checkpoint.read_text(encoding="utf-8"))
        if recovered["sha"] != plan["sha"]:
            raise ValueError("Recovery patch does not match the approved commit")
        active_patch = ROOT / "core-recovery.patch"
        active_patch.write_text(recovered["patch"], encoding="utf-8", newline="\n")
    try:
        if active_patch.stat().st_size:
            conflicts = apply_integration_patch(work, active_patch)
            if conflicts:
                repair_failure(RuntimeError("Integration conflicts: " + ", ".join(conflicts)), "patch", conflicts)
    except RuntimeError as exc:
        failure_context(plan, store, work, review=review, stage="patch", error=str(exc))
        raise
    # Integration can change both .gitmodules and pinned dependency revisions.
    command(["git", "submodule", "sync", "--recursive"], work, 120)
    command(["git", "submodule", "update", "--init", "--recursive", "--depth=1"], work, 1800)
    for subpatch in sorted((source / "submodules").glob("*.patch")):
        subpatch.write_bytes(subpatch.read_bytes().replace(b"\r\n", b"\n"))
        relative = json.loads((source / "submodules.json").read_text())[subpatch.name]
        location = (work / relative).resolve()
        location.relative_to(work.resolve())
        command(["git", "apply", str(subpatch.resolve())], location, 60)
    args = ["cmake", "-S", ".", "-B", "catui-build", "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"] + recipe["cmake"]
    # Paths are controlled runner settings, never LLM/upstream message inputs.
    for key in ("CMAKE_PREFIX_PATH", "CMAKE_TOOLCHAIN_FILE", "Qt6_DIR", "LLVM_DIR",
                "VCPKG_INSTALLED_DIR", "VCPKG_TARGET_TRIPLET", "VCPKG_MANIFEST_DIR", "VCPKG_MANIFEST_INSTALL"):
        if os.environ.get(key):
            args.append(f"-D{key}={os.environ[key]}")
    manifest = json.loads((core_dir / "manifest.json").read_text())
    manifest.update(version=plan["version"], upstreamCommit=plan["sha"])
    filename = manifest.get("filename", core + "_libretro.dll")
    package = output / filename
    stage = work / "catui-package"
    while True:
        log_start = Path("build.log").stat().st_size if Path("build.log").exists() else 0
        build_stage = "compile"
        try:
            for path in apply_known_fixes(core, work):
                command(["git", "add", "--", path], work, 30)
                if path not in source_fix_files:
                    source_fix_files.append(path)
            if stage.exists():
                shutil.rmtree(stage)
            if output.exists():
                shutil.rmtree(output)
            print("Build phase: configuring and compiling the approved source", flush=True)
            command(args, work, 1800)
            command(["cmake", "--build", "catui-build", "--config", "Release", "--parallel", "4", "--target", *recipe["targets"]], work, 10800)
            for extra in recipe.get("extra", []):
                command(extra, work, 1800)
            output.mkdir(parents=True)
            stage.mkdir()
            for item in recipe["outputs"]:
                origin = find_one(work / item.get("search", "catui-build"), item["name"])
                target = stage / item["destination"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, target)
            if recipe.get("qtDeploy"):
                qt_bin = Path(os.environ["Qt6_DIR"]).resolve().parents[2] / "bin"
                host = stage / recipe["qtDeploy"]
                command([qt_bin / "windeployqt.exe", "--release", "--no-translations", "--no-compiler-runtime", str(host)], work, 180)
            for location in recipe.get("runtimeDirectories", []):
                for library in (work / location).glob("*.dll"):
                    if re.match(r"Qt6.*d\.dll$", library.name):
                        continue
                    destination = stage / recipe.get("runtimeDestination", "") / library.name
                    if not destination.exists():
                        shutil.copy2(library, destination)
            if manifest.get("packageType") == "archive":
                license_target = stage / Path(manifest["entryFilename"]).parent / "LICENSE"
                shutil.copy2(source / "LICENSE", license_target)
            entry = stage / manifest.get("entryFilename", filename)
            imports = single_dll_check(entry) if core == "pcsx2" else []
            build_stage = "abi"
            print("Build phase: checking the compiled DLL ABI", flush=True)
            log_start = Path("build.log").stat().st_size
            command([sys.executable, str(Path(__file__).resolve()), "--abi", str(entry.resolve())], Path.cwd(), 45)
            # Required external gate: graphics/audio/lifecycle, not just exported symbols.
            gate = Path(os.environ.get("CORE_VALIDATION_SCRIPT", "validation/validate.py")).resolve()
            if not gate.is_file():
                raise RuntimeError("Runtime validation is not configured; refusing to publish an ABI-only build")
            build_stage = "validation"
            print("Build phase: native and CATui runtime validation", flush=True)
            log_start = Path("build.log").stat().st_size
            command([sys.executable, str(gate), "--core", core, "--package-dir", str(stage.resolve()),
                     "--report", str((output / "validation.json").resolve())], Path.cwd(), 900)
            report = json.loads((output / "validation.json").read_text())
            if report.get("core") != core or report.get("passed") is not True:
                raise ValueError("Runtime validation failed")
            break
        except (RuntimeError, ValueError) as exc:
            # Previous patch/compile errors must not crowd out the current failure.
            if Path("build.log").exists():
                Path("attempt.log").write_bytes(Path("build.log").read_bytes()[log_start:])
            repair_failure(exc, build_stage)
    if manifest.get("packageType") == "archive":
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())
    else:
        shutil.copy2(entry, package)
    digest = inspect_package(package, manifest)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    shutil.copytree(source, output / "source")
    (output / "source/build.json").write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")
    integration = metadata["integrationDirectory"]
    if (output / "source/integration").exists():
        shutil.rmtree(output / "source/integration")
    shutil.copytree(work / integration, output / "source/integration",
                    ignore=shutil.ignore_patterns(".git", "*.dll", "*.exe", "__pycache__"))
    command(["git", "add", "-u"], work, 60)
    # All original integration additions came from the index patch; no build output is added.
    patch_text = command(["git", "diff", "--cached", "--binary", plan["sha"], "--"], work, 120)
    (output / "source/integration.patch").write_text(patch_text + "\n", encoding="utf-8", newline="\n")
    metadata["commit"] = plan["sha"]
    metadata["changedFiles"] = command(["git", "diff", "--cached", "--name-only", plan["sha"]], work, 60).splitlines()
    metadata["snapshot"] = snapshot(work, output / "source", additions=metadata["changedFiles"])
    metadata["packageBaselineVerified"] = True
    metadata["package"] = {"filename": filename, "sha256": digest}
    (output / "source/upstream.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n")
    receipt = {"plan": plan, "sha256": digest, "size": package.stat().st_size,
               "filename": filename, "abi": True, "runtime": report,
               "systemImports": imports,
               "integrationReview": review,
               "sourceFixFiles": source_fix_files,
               "aiRepairFiles": repaired_files}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Built and validated {core}; SHA-256 {digest}")


def failure_context(plan, store, work, review=None, stage="build", error=""):
    source = store / "store/cores" / plan["core"] / "source"
    metadata = json.loads((source / "upstream.json").read_text())
    log_path = Path("attempt.log") if stage in {"compile", "abi", "validation"} and Path("attempt.log").exists() else Path("build.log")
    log = log_path.read_text(encoding="utf-8", errors="replace")[-30000:] if log_path.exists() else "Build failed before compiler output"
    files = []
    budget = MAX_SOURCE + 60000
    conflicts = []
    upstream_diff = ""
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("GITHUB_", "ACTIONS_"))
                   and not any(word in k.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "SIGNING", "API_KEY"))}
    if (work / ".git").exists():
        result = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=work,
                                capture_output=True, text=True, timeout=30, env=environment)
        conflicts = result.stdout.splitlines()
    log_paths = extract_log_paths(log + "\n" + error, work) if work.exists() else []
    prioritized = prioritize_integration_paths(metadata["changedFiles"])
    allowed_candidates = set(metadata["changedFiles"]) | set(conflicts) | set(log_paths)
    integration = metadata.get("integrationDirectory", "libretro")
    runtime_paths = [integration + "/" + name for name in
                     ("libretro_core.cpp", "environment.cpp", "libretro.cpp", "CemuLibretro.cpp", "modern_main.cpp", "modern_host.cpp")]
    if stage in {"abi", "validation"}:
        allowed_candidates.update(runtime_paths)
        prioritized = runtime_paths + prioritized
        # Compiler progress filenames are irrelevant to a native lifecycle crash.
        log_paths = extract_log_paths("\n".join(line for line in log.splitlines() if "error" in line.lower()), work)
    build_paths = ["CMakeLists.txt", "src/CMakeLists.txt", integration + "/CMakeLists.txt"]
    # An unknown target needs the CMake entry points, not stale Vulkan conflicts.
    paths = list(dict.fromkeys(conflicts + (build_paths if "unknown target" in log else []) + log_paths + prioritized + build_paths))
    allowed_candidates.update(build_paths)
    for name in paths:
        if name not in allowed_candidates or not is_editable_source(work, name):
            continue
        path = (work / name).resolve()
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_SOURCE or len(text) > budget:
            continue
        budget -= len(text)
        files.append({"path": name.replace("\\", "/"), "content": text})
        if len(files) >= 16:
            break
    if (work / ".git").exists() and re.fullmatch(r"[a-f0-9]{40}", metadata.get("commit", "")):
        diff_targets = [p for p in prioritized if not p.startswith(("libretro/libretro-common/", "libretro/libretro-usb/"))][:24]
        for extra in log_paths:
            if extra not in diff_targets:
                diff_targets.append(extra)
        if plan["core"] == "pcsx2":
            for header in ("pcsx2/Host.h", "pcsx2/VMManager.h", "pcsx2/GS/GS.h", "pcsx2/Config.h"):
                if header not in diff_targets:
                    diff_targets.append(header)
        if diff_targets:
            diff_res = subprocess.run(
                ["git", "diff", "--unified=3", metadata["commit"], plan["sha"], "--", *diff_targets],
                cwd=work, capture_output=True, text=True, timeout=60, env=environment)
            if diff_res.returncode == 0:
                upstream_diff = diff_res.stdout[:40000]
    payload = {
        "core": plan["core"],
        "sha": plan["sha"],
        "stage": stage,
        "error": error[:2000],
        "conflicts": conflicts,
        "log": log,
        "upstreamDiff": upstream_diff,
        "review": {"decision": review.get("decision"), "summary": review.get("summary"), "risks": review.get("risks", [])}
        if isinstance(review, dict) else None,
        "files": files,
    }
    Path("failure-context.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--abi")
    parser.add_argument("--store", default="store-repo")
    parser.add_argument("--work", default="core-work")
    parser.add_argument("--output", default="core-output")
    args = parser.parse_args()
    if args.abi:
        abi_check(Path(args.abi))
    else:
        plan = json.loads(os.environ["APPROVED_PLAN"])
        validate_plan(plan)
        try:
            build(plan, Path(args.store).resolve(), Path(args.work).resolve(), Path(args.output).resolve())
        except Exception as exc:
            print(f"Build stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
            traceback.print_exc()
            # Preserve the last repair context instead of replacing its stage,
            # attempt history and review with a generic final "build" context.
            context_path = Path("failure-context.json")
            try:
                context = json.loads(context_path.read_text(encoding="utf-8")) if context_path.is_file() else {}
                if context.get("sha") == plan["sha"]:
                    context["error"] = str(exc)[:2000]
                    context_path.write_text(json.dumps(context), encoding="utf-8")
                else:
                    failure_context(plan, Path(args.store).resolve(), Path(args.work).resolve(), error=str(exc))
            except Exception as context_exc:
                print(f"Diagnostic capture failed: {type(context_exc).__name__}", file=sys.stderr)
            sys.exit(1)
